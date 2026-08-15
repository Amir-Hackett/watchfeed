#!/usr/bin/env python3
"""
watchfeed — one .ics + one .rss for everything you watch and play.

Pulls from:
  TVmaze   TV episode air dates          no key
  AniList  anime episode air dates       no key
  TMDB     movie release dates           TMDB_API_KEY
  IGDB     game release dates            TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET

Zero third-party dependencies. Python 3.11+ (uses tomllib).

  python3 watchfeed.py --config config.toml --out ./out
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UA = "watchfeed/1.0 (+https://github.com/Amir-Hackett/watchfeed)"
TIMEOUT = 20


# ---------------------------------------------------------------- model


@dataclass
class Release:
    """One dated thing. Everything normalizes into this."""

    title: str          # "Lioness"
    detail: str         # "S03E02 - No Sorrow Like the Survivor"
    on: date
    kind: str           # tv | anime | movie | game
    source: str         # tvmaze | anilist | tmdb | igdb
    url: str = ""
    platform: str = ""  # Paramount+, PS5, theaters...
    exact_time: datetime | None = None   # set only when source gives a real UTC time
    tags: list[str] = field(default_factory=list)
    image: str = ""     # poster/cover URL, landing page only
    about: str = ""     # short description, landing page only
    recap: str = ""     # "Previously: S03E05 — ..." line, landing page only

    @property
    def summary(self) -> str:
        icon = {"tv": "TV", "anime": "ANIME", "movie": "FILM", "game": "GAME"}[self.kind]
        head = f"[{icon}] {self.title}"
        return f"{head} — {self.detail}" if self.detail else head

    @property
    def uid(self) -> str:
        """Stable across runs so the calendar updates instead of duplicating."""
        raw = f"{self.source}|{self.kind}|{self.title}|{self.detail}|{self.on}"
        return hashlib.sha1(raw.encode()).hexdigest() + "@watchfeed"


# ---------------------------------------------------------------- cache
#
# Last-known-good snapshot per source, committed under <out>/cache/ by CI.
# When an upstream API goes down wholesale (e.g. AniList disabling its API),
# a rebuild would otherwise publish a feed with that whole category missing
# and subscribed calendars would drop those events.

_CACHE_MAX_AGE_DAYS = 14  # a source down longer than this is stale enough to drop


def _save_cache(out: Path, source: str, rels: "list[Release]") -> None:
    path = out / "cache" / f"{source}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "releases": [{
            "title": r.title, "detail": r.detail, "on": r.on.isoformat(),
            "kind": r.kind, "source": r.source, "url": r.url,
            "platform": r.platform,
            "exact_time": r.exact_time.isoformat() if r.exact_time else None,
            "tags": r.tags, "image": r.image, "about": r.about, "recap": r.recap,
        } for r in rels],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_cache(out: Path, source: str) -> "list[Release]":
    path = out / "cache" / f"{source}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
        fetched = datetime.fromisoformat(payload["fetched"])
    except (ValueError, KeyError) as e:
        _warn(f"cache: unreadable {path.name} ({e})")
        return []
    age = datetime.now(timezone.utc) - fetched
    if age > timedelta(days=_CACHE_MAX_AGE_DAYS):
        _warn(f"cache: {path.name} is {age.days}d old — too stale to reuse")
        return []
    return [Release(
        title=e["title"], detail=e["detail"], on=date.fromisoformat(e["on"]),
        kind=e["kind"], source=e["source"], url=e.get("url", ""),
        platform=e.get("platform", ""),
        exact_time=datetime.fromisoformat(e["exact_time"]) if e.get("exact_time") else None,
        tags=e.get("tags") or [], image=e.get("image", ""),
        about=e.get("about", ""), recap=e.get("recap", ""),
    ) for e in payload.get("releases", [])]


# ---------------------------------------------------------------- http


def _get(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 429 = rate limited. Honor Retry-After, else back off; anything else is fatal.
            if e.code == 429 and attempt < 2:
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(wait or 2 ** attempt * 3)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def _warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def _clean(s: str | None, limit: int = 420) -> str:
    """Strip HTML tags/entities from API summaries and cap the length."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\(Source:.*?\)\s*$", "", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return s


# Fetchers keep a trailing week so the page can show recent history and so
# every viewer's local "today" is in the data regardless of build-time UTC.
# The ics/rss feeds stay upcoming-only; main() filters before writing them.
_PAST_DAYS = 7


def _since() -> date:
    return date.today() - timedelta(days=_PAST_DAYS)


# ---------------------------------------------------------------- tvmaze


def fetch_tvmaze(titles: list[str], horizon: date) -> list[Release]:
    out: list[Release] = []
    today = date.today()
    since = _since()
    for t in titles:
        try:
            q = urllib.parse.urlencode({"q": t})
            show = _get(f"https://api.tvmaze.com/singlesearch/shows?{q}")
        except Exception as e:
            _warn(f"tvmaze: no match for {t!r} ({e})")
            continue

        try:
            eps = _get(f"https://api.tvmaze.com/shows/{show['id']}/episodes")
        except Exception as e:
            _warn(f"tvmaze: episode fetch failed for {t!r} ({e})")
            continue

        chan = show.get("network") or show.get("webChannel") or {}
        net = chan.get("name", "")
        net_tz = (chan.get("country") or {}).get("timezone")
        img = (show.get("image") or {}).get("medium", "")
        about = _clean(show.get("summary"))
        aired = [e for e in eps if e.get("airdate") and e["airdate"] < today.isoformat()]
        recap = ""
        if aired:
            last = max(aired, key=lambda e: e["airdate"])
            lab = (f"S{last['season']:02d}E{last['number']:02d}"
                   if last.get("number") else "Special")
            if last.get("name"):
                lab += f" — {last['name']}"
            recap = f"Previously: {lab} · {date.fromisoformat(last['airdate']):%b %-d}"
        found = 0
        for ep in eps:
            if not ep.get("airdate"):
                continue
            d = date.fromisoformat(ep["airdate"])
            if not (since <= d <= horizon):
                continue
            when = None
            if ep.get("airstamp"):
                try:
                    when = datetime.fromisoformat(ep["airstamp"]).astimezone(timezone.utc)
                except ValueError:
                    pass
            # Post-midnight blocks (Toonami etc.): TVMaze keeps the TV-guide
            # airdate but stamps the real instant on the next calendar day.
            # An exact time would land the calendar event a day after the
            # site's listing, so fall back to an all-day event on the airdate.
            if when and net_tz:
                try:
                    if when.astimezone(ZoneInfo(net_tz)).date() != d:
                        when = None
                except (ZoneInfoNotFoundError, ValueError):
                    pass
            label = f"S{ep['season']:02d}E{ep['number']:02d}" if ep.get("number") else "Special"
            if ep.get("name"):
                label += f" — {ep['name']}"
            out.append(Release(
                title=show["name"], detail=label, on=d, kind="tv", source="tvmaze",
                url=show.get("url", ""), platform=net, exact_time=when,
                image=img, about=about, recap=recap,
            ))
            found += 1
        print(f"  tvmaze  {show['name']}: {found}")
    return out


# ---------------------------------------------------------------- anilist

_ANILIST_FIELDS = """
    id
    title { romaji english }
    siteUrl
    status
    description(asHtml: false)
    coverImage { large }
    airingSchedule(notYetAired: true, perPage: 50) {
      nodes { episode airingAt }
    }
    relations {
      edges { relationType node { id type } }
    }
"""

ANILIST_Q = f"query ($search: String) {{ Media(search: $search, type: ANIME) {{{_ANILIST_FIELDS}}} }}"
ANILIST_ID_Q = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{{_ANILIST_FIELDS}}} }}"

# Media.airingSchedule(notYetAired: true) omits aired episodes, so the trailing
# week comes from a separate windowed query against the top-level connection.
ANILIST_PAST_Q = (
    "query ($id: Int, $lo: Int, $hi: Int) { Page(perPage: 25) { "
    "airingSchedules(mediaId: $id, airingAt_greater: $lo, airingAt_lesser: $hi, "
    "sort: TIME) { episode airingAt } } }"
)


def _anilist_past(mid: int) -> list[dict]:
    lo = int(time.mktime(_since().timetuple()))
    body = json.dumps({"query": ANILIST_PAST_Q, "variables": {
        "id": mid, "lo": lo, "hi": int(time.time())}}).encode()
    resp = _get("https://graphql.anilist.co", data=body)
    return ((resp.get("data") or {}).get("Page") or {}).get("airingSchedules") or []


def _anilist_media(query: str, variables: dict) -> dict | None:
    body = json.dumps({"query": query, "variables": variables}).encode()
    resp = _get("https://graphql.anilist.co", data=body)
    return (resp.get("data") or {}).get("Media")


def _sequel_id(m: dict, seen: set[int]) -> int | None:
    edges = (m.get("relations") or {}).get("edges") or []
    for e in edges:
        node = e.get("node") or {}
        if e.get("relationType") == "SEQUEL" and node.get("type") == "ANIME" \
                and node.get("id") not in seen:
            return node["id"]
    return None


def fetch_anilist(titles: list[str], horizon: date) -> list[Release]:
    out: list[Release] = []
    today = date.today()
    for t in titles:
        try:
            m = _anilist_media(ANILIST_Q, {"search": t})
        except Exception as e:
            _warn(f"anilist: lookup failed for {t!r} ({e})")
            continue
        if not m:
            _warn(f"anilist: no match for {t!r}")
            continue

        # Walk the sequel chain so one entry tracks every future season.
        seen = {m["id"]}
        for hop in range(15):  # runaway guard; real chains are short
            name = m["title"].get("english") or m["title"]["romaji"]
            nodes = (m.get("airingSchedule") or {}).get("nodes") or []
            img = (m.get("coverImage") or {}).get("large", "")
            about = _clean(m.get("description"))
            found = 0
            for n in nodes:
                when = datetime.fromtimestamp(n["airingAt"], tz=timezone.utc)
                d = when.date()
                if not (today <= d <= horizon):
                    continue
                out.append(Release(
                    title=name, detail=f"Episode {n['episode']}", on=d, kind="anime",
                    source="anilist", url=m.get("siteUrl", ""), exact_time=when,
                    image=img, about=about,
                ))
                found += 1
            if m.get("status") == "RELEASING":
                time.sleep(2.1)
                try:
                    aired = _anilist_past(m["id"])
                except Exception as e:
                    _warn(f"anilist: past-week fetch failed for {name!r} ({e})")
                    aired = []
                for n in aired:
                    when = datetime.fromtimestamp(n["airingAt"], tz=timezone.utc)
                    out.append(Release(
                        title=name, detail=f"Episode {n['episode']}", on=when.date(),
                        kind="anime", source="anilist", url=m.get("siteUrl", ""),
                        exact_time=when, image=img, about=about,
                    ))
                    found += 1
            if hop == 0 or found:
                print(f"  anilist {name}{' (sequel)' if hop else ''}: {found}")
            if not nodes and m.get("status") == "NOT_YET_RELEASED":
                _warn(f"anilist: {name} announced but no schedule yet")

            nxt = _sequel_id(m, seen)
            time.sleep(2.1)  # AniList throttles to ~30 req/min; sequel hops add requests.
            if nxt is None:
                break
            seen.add(nxt)
            try:
                m = _anilist_media(ANILIST_ID_Q, {"id": nxt})
            except Exception as e:
                _warn(f"anilist: sequel fetch failed after {name!r} ({e})")
                break
            if not m:
                break
    return out


# ---------------------------------------------------------------- tmdb


def fetch_tmdb(titles: list[str], horizon: date, key: str) -> list[Release]:
    out: list[Release] = []
    today = date.today()
    for t in titles:
        try:
            q = urllib.parse.urlencode({"api_key": key, "query": t})
            res = _get(f"https://api.themoviedb.org/3/search/movie?{q}")
        except Exception as e:
            _warn(f"tmdb: search failed for {t!r} ({e})")
            continue

        hits = res.get("results") or []
        if not hits:
            _warn(f"tmdb: no match for {t!r}")
            continue

        # Prefer the soonest release still in the window; else most popular.
        since = _since()
        future = [h for h in hits if h.get("release_date") and h["release_date"] >= since.isoformat()]
        m = min(future, key=lambda h: h["release_date"]) if future else hits[0]

        if not m.get("release_date"):
            _warn(f"tmdb: {t!r} has no date set")
            continue
        d = date.fromisoformat(m["release_date"])
        if not (since <= d <= horizon):
            print(f"  tmdb    {m['title']}: 0 (date {d} outside window)")
            continue

        img = (f"https://image.tmdb.org/t/p/w342{m['poster_path']}"
               if m.get("poster_path") else "")
        out.append(Release(
            title=m["title"], detail="Theatrical release", on=d, kind="movie",
            source="tmdb", url=f"https://www.themoviedb.org/movie/{m['id']}",
            platform="Theaters", image=img, about=_clean(m.get("overview")),
        ))
        print(f"  tmdb    {m['title']}: {d}")
    return out


# ---------------------------------------------------------------- igdb


def igdb_token(client_id: str, secret: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": secret,
        "grant_type": "client_credentials",
    })
    req = urllib.request.Request(
        f"https://id.twitch.tv/oauth2/token?{q}", data=b"", headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())["access_token"]


def _igdb_query(body: str, client_id: str, token: str) -> list:
    req = urllib.request.Request(
        "https://api.igdb.com/v4/games",
        data=body.encode(),
        headers={
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def fetch_igdb(titles: list[str], horizon: date, client_id: str, secret: str) -> list[Release]:
    try:
        token = igdb_token(client_id, secret)
    except Exception as e:
        _warn(f"igdb: auth failed ({e}) — skipping games")
        return []

    out: list[Release] = []
    today = date.today()
    lo, hi = int(time.mktime(_since().timetuple())), int(time.mktime(horizon.timetuple()))

    for t in titles:
        safe = t.replace('"', '')
        body = (
            f'search "{safe}"; '
            f"fields name,url,summary,cover.image_id,"
            f"release_dates.date,release_dates.human,"
            f"release_dates.platform.abbreviation; limit 5;"
        )
        try:
            games = _igdb_query(body, client_id, token)
        except Exception as e:
            _warn(f"igdb: search failed for {t!r} ({e})")
            continue

        if not games:
            _warn(f"igdb: no match for {t!r}")
            continue

        g = games[0]
        dates = g.get("release_dates") or []
        # Earliest dated release inside the window, across platforms.
        cands = [rd for rd in dates if rd.get("date") and lo <= rd["date"] <= hi]
        if not cands:
            _warn(f"igdb: {g['name']} has no confirmed date in window (likely TBA)")
            continue

        rd = min(cands, key=lambda x: x["date"])
        d = datetime.fromtimestamp(rd["date"], tz=timezone.utc).date()
        plats = sorted({
            (x.get("platform") or {}).get("abbreviation", "")
            for x in cands if x["date"] == rd["date"]
        } - {""})

        cov = (g.get("cover") or {}).get("image_id")
        img = (f"https://images.igdb.com/igdb/image/upload/t_cover_big/{cov}.jpg"
               if cov else "")
        out.append(Release(
            title=g["name"], detail="Release day", on=d, kind="game", source="igdb",
            url=g.get("url", ""), platform=", ".join(plats),
            image=img, about=_clean(g.get("summary")),
        ))
        print(f"  igdb    {g['name']}: {d} ({', '.join(plats) or 'platform n/a'})")
        time.sleep(0.3)  # IGDB: 4 req/sec
    return out


# ---------------------------------------------------------------- ics


def _esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", r"\;")
             .replace(",", r"\,").replace("\n", r"\n"))


def _fold(line: str) -> str:
    """RFC 5545 wants <=75 octets per line. Fold on bytes, not chars."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not parts else 74  # continuation lines carry a leading space
        if len(cur) + len(b) > limit:
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    if cur:
        parts.append(cur.decode("utf-8"))
    return parts[0] + "".join("\r\n " + p for p in parts[1:])


def build_ics(rels: list[Release], cal_name: str, alarm_min: int, all_day: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//watchfeed//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(cal_name)}",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    for r in sorted(rels, key=lambda x: (x.on, x.title)):
        L += ["BEGIN:VEVENT", f"UID:{r.uid}", f"DTSTAMP:{now}"]

        if r.exact_time and not all_day:
            start = r.exact_time.strftime("%Y%m%dT%H%M%SZ")
            end = (r.exact_time + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
            L += [f"DTSTART:{start}", f"DTEND:{end}"]
        else:
            # All-day events are DTEND-exclusive: end is the *next* day.
            L += [
                f"DTSTART;VALUE=DATE:{r.on.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(r.on + timedelta(days=1)).strftime('%Y%m%d')}",
            ]

        L.append(f"SUMMARY:{_esc(r.summary)}")
        desc = " / ".join(x for x in [r.platform, r.url] if x)
        if desc:
            L.append(f"DESCRIPTION:{_esc(desc)}")
        if r.url:
            L.append(f"URL:{r.url}")
        if r.platform:
            L.append(f"LOCATION:{_esc(r.platform)}")
        L += ["TRANSP:TRANSPARENT", f"CATEGORIES:{r.kind.upper()}"]

        if alarm_min > 0:
            L += [
                "BEGIN:VALARM", "ACTION:DISPLAY",
                f"TRIGGER:-PT{alarm_min}M",
                f"DESCRIPTION:{_esc(r.summary)}",
                "END:VALARM",
            ]
        L.append("END:VEVENT")

    L.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


# ---------------------------------------------------------------- html


def _xesc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_KIND_LABEL = {"tv": "TV", "anime": "Anime", "movie": "Film", "game": "Game"}


def _rel_label(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    if n < 0:
        return f"{-n} days ago"
    if n < 14:
        return f"in {n} days"
    if n < 61:
        return f"in {n // 7} weeks"
    return f"in {round(n / 30.4)} months"


# Light tokens apply in two contexts (system-light with no override, and an
# explicit data-theme="light"), so they live here once and are spliced in twice.
_LIGHT_VARS = """
    color-scheme: light; --show-sun: none; --show-moon: block;
    --bg: #f3f1f8; --glow: rgba(124,92,240,.09);
    --surface: #ffffff; --surface-2: #faf8ff;
    --text: #1c1728; --muted: #565165; --faint: #8a8599;
    --line: rgba(28,22,52,.12);
    --accent: #6d4de0; --accent-fg: #ffffff;
    --accent-soft: rgba(109,77,224,.1); --accent-bd: rgba(109,77,224,.4);
    --card-shadow: 0 1px 2px rgba(28,22,52,.06), 0 10px 24px -14px rgba(28,22,52,.22);
    --tv: #2563eb; --anime: #7c3aed; --movie: #b45309; --game: #0c8f66;
"""

_CSS = """
:root {
  color-scheme: dark; --show-sun: block; --show-moon: none;
  --bg: #0a0a10; --glow: rgba(124,92,240,.14);
  --surface: #14141d; --surface-2: #1c1c28;
  --text: #ececf4; --muted: #a6a6bc; --faint: #73738c;
  --line: rgba(255,255,255,.09);
  --accent: #a78bfa; --accent-fg: #14101f;
  --accent-soft: rgba(167,139,250,.14); --accent-bd: rgba(167,139,250,.45);
  --card-shadow: none;
  --tv: #60a5fa; --anime: #c4b5fd; --movie: #fbbf24; --game: #34d399;
  --serif: ui-serif, Georgia, "Iowan Old Style", "Times New Roman", serif;
}
:root[data-theme="light"] {@LIGHT@}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {@LIGHT@}
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: max(28px, env(safe-area-inset-top)) clamp(18px, 4vw, 44px)
    max(48px, env(safe-area-inset-bottom));
  background: var(--bg);
  background-image: radial-gradient(ellipse 90% 45% at 50% -8%, var(--glow), transparent);
  color: var(--text);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
::selection { background: var(--accent); color: var(--accent-fg); }
header, main, footer { max-width: 1120px; margin: 0 auto; }
.overline {
  display: flex; align-items: center; gap: 10px; margin: 0 0 4px;
  font-size: 12.5px; font-weight: 700; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--accent);
}
.overline::before { content: ""; width: 28px; height: 2px; border-radius: 2px;
  background: var(--accent); }
h1 {
  margin: 6px 0 10px; font-family: var(--serif);
  font-size: clamp(42px, 9vw, 58px); font-weight: 700;
  letter-spacing: -0.015em; line-height: 1.05; color: var(--text);
}
h1 .tld { color: var(--accent); }
.tag { margin: 0 0 8px; font-size: 17px; color: var(--muted); max-width: 46ch; }
.updated {
  margin: 0 0 24px; font-size: 12.5px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--faint); font-variant-numeric: tabular-nums;
}
.feeds { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.btn, .chip {
  display: inline-flex; align-items: center; min-height: 44px; padding: 0 18px;
  border-radius: 999px; text-decoration: none; font-weight: 600; font-size: 14.5px;
  transition: transform .15s ease, background .15s ease,
    border-color .15s ease, color .15s ease;
}
.btn { background: var(--accent); color: var(--accent-fg); }
.btn:hover { transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.chip { border: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.chip:hover { border-color: var(--accent); color: var(--accent);
  transform: translateY(-1px); }
.url { margin-top: 14px; font-size: 13px; color: var(--faint); overflow-wrap: anywhere; }
.url code { font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 4px 8px; }
a { color: var(--accent); }
a:focus-visible, .btn:focus-visible, .chip:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
.search {
  position: sticky; top: 14px; z-index: 20;
  display: flex; align-items: center; gap: 10px;
  margin: 30px 0 2px; padding: 0 18px; min-height: 52px;
  background: color-mix(in srgb, var(--surface) 82%, transparent);
  -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  border: 1px solid var(--line); border-radius: 999px;
  box-shadow: var(--card-shadow);
}
.search:focus-within { border-color: var(--accent-bd); }
.search svg { width: 17px; height: 17px; color: var(--faint); flex: none; }
.search input {
  flex: 1; min-width: 0; border: 0; background: none; color: var(--text);
  font: inherit; font-size: 16px; padding: 13px 0; outline: none;
}
.search input::placeholder { color: var(--faint); }
.search input::-webkit-search-cancel-button { -webkit-appearance: none; }
.search kbd {
  flex: none; font: 11.5px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--faint); background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px 7px;
}
#theme {
  flex: none; display: flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; margin-right: -8px; padding: 0;
  border: 0; border-radius: 999px; background: none; color: var(--faint);
  cursor: pointer; transition: color .15s ease, background .15s ease;
}
#theme:hover { color: var(--accent); background: var(--accent-soft); }
#theme:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
#theme svg { width: 18px; height: 18px; }
#theme .sun { display: var(--show-sun); }
#theme .moon { display: var(--show-moon); }
#today {
  flex: none; border: 0; background: var(--accent-soft); color: var(--accent);
  font: inherit; font-size: 12px; font-weight: 700; letter-spacing: .05em;
  text-transform: uppercase; padding: 7px 12px; border-radius: 999px; cursor: pointer;
  transition: background .15s ease, color .15s ease;
}
#today:hover { background: var(--accent); color: var(--accent-fg); }
#today:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.day { margin-top: 30px; scroll-margin-top: 84px; }
.day.past { opacity: .55; transition: opacity .2s ease; }
.day.past:hover, .day.past:focus-within { opacity: 1; }
h2 {
  display: flex; align-items: baseline; gap: 10px; margin: 0 0 12px;
  font-size: 13.5px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--muted); font-variant-numeric: tabular-nums;
}
h2::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.rel { font-weight: 700; font-size: 12.5px; letter-spacing: 0.1em;
  color: var(--accent); }
.grid { list-style: none; margin: 0; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card {
  position: relative; height: 176px; perspective: 1200px; cursor: pointer;
  transition: transform .18s ease, height .55s cubic-bezier(.3,.8,.3,1);
  -webkit-tap-highlight-color: transparent;
}
/* Grow while flipped so the description is readable without a tiny scroll box. */
.card.flipped { height: 280px; }
.card:hover { transform: translateY(-2px); }
.card:active { transform: scale(.985); }
.card:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.card.tv { --rail: var(--tv); }
.card.anime { --rail: var(--anime); }
.card.movie { --rail: var(--movie); }
.card.game { --rail: var(--game); }
.card .inner {
  position: absolute; inset: 0; transform-style: preserve-3d;
  transition: transform .55s cubic-bezier(.3,.8,.3,1);
}
.card.flipped .inner { transform: rotateY(180deg); }
.face {
  position: absolute; inset: 0; overflow: hidden;
  display: flex; flex-direction: column; gap: 6px;
  padding: 14px 18px 14px 20px;
  background: var(--surface);
  border: 1px solid var(--line); border-radius: 4px 14px 14px 4px;
  box-shadow: var(--card-shadow);
  -webkit-backface-visibility: hidden; backface-visibility: hidden;
}
.face::before {
  content: ""; position: absolute; left: -1px; top: -1px; bottom: -1px;
  width: 3px; border-radius: 4px 0 0 4px;
  background: var(--rail); opacity: .75;
}
.card:hover .face { border-color: var(--accent-bd); }
.card:hover .face::before { opacity: 1; }
.today .face { border-color: var(--accent-bd);
  background:
    linear-gradient(180deg, transparent, var(--accent-soft)),
    var(--surface); }
.front { padding-right: 124px; }
.poster {
  position: absolute; right: 12px; top: 12px;
  width: 100px; height: calc(100% - 24px);
  object-fit: cover; border-radius: 10px; background: var(--surface-2);
}
span.poster { display: flex; align-items: center; justify-content: center; }
.ph { color: var(--rail); background: color-mix(in srgb, var(--rail) 14%, transparent); }
.ph svg { width: 28px; height: 28px; opacity: .85; }
.meta {
  margin: 0; display: flex; align-items: center; gap: 7px;
  flex-wrap: nowrap; white-space: nowrap; overflow: hidden;
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--faint);
}
.meta .cat { color: var(--rail); }
.meta .sep { opacity: .5; }
.t {
  margin: 0; font-family: var(--serif); font-size: 17px; font-weight: 700;
  line-height: 1.3; letter-spacing: -0.005em; color: var(--text);
  display: -webkit-box; -webkit-line-clamp: 3;
  -webkit-box-orient: vertical; overflow: hidden;
}
.snippet {
  margin: 0; color: var(--muted); font-size: 13.5px; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
/* Centre the text block in whatever height is left so a short title doesn't
   sit above a dead void (scoutfeed's .no-snippet trick). */
.front .t { margin-top: auto; }
.front .snippet { margin-bottom: auto; }
.front .t:last-child { margin-bottom: auto; }
.hint {
  position: absolute; right: 124px; bottom: 11px; width: 20px; height: 20px;
  color: var(--faint); opacity: .5; transition: opacity .15s ease, color .15s ease;
}
.card:hover .hint { opacity: 1; color: var(--accent); }
.back { transform: rotateY(180deg); padding: 14px 18px;
  display: flex; flex-direction: column; gap: 7px; }
.back .t { -webkit-line-clamp: 2; font-size: 15.5px; }
.back .hint { right: 11px; }
.recap { margin: 0; font-size: 12.5px; font-weight: 700; letter-spacing: 0.02em;
  color: var(--accent); }
.about { margin: 0; flex: 1; overflow: auto; overscroll-behavior: contain;
  scrollbar-width: thin; font-size: 14.5px; line-height: 1.6; color: var(--muted);
  /* fade the cutoff so a clipped line reads as "scroll for more", not a bug */
  mask-image: linear-gradient(180deg, #000 calc(100% - 16px), transparent); }
.more { align-self: flex-start; font-size: 11.5px; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent);
  text-decoration: none; }
.more:hover { text-decoration: underline; }
.hide { display: none !important; }
#empty { margin: 40px 0; padding: 28px; text-align: center; color: var(--faint);
  border: 1px dashed var(--line); border-radius: 16px; font-size: 14.5px; }
footer { margin-top: 48px; font-size: 13px; color: var(--faint); }
footer a { color: var(--faint); text-underline-offset: 3px; }
footer a:hover { color: var(--accent); }
@media (hover: none) {
  .search kbd { display: none; }
}
@media (max-width: 540px) {
  .tag { font-size: 16px; }
  .updated { margin-bottom: 20px; }
  .feeds { gap: 8px; }
  .btn { flex: 1 1 100%; justify-content: center; }
  .chip { flex: 1 1 30%; justify-content: center; }
  .url code { display: block; margin-top: 6px; padding: 8px 10px; }
  .search { top: max(10px, env(safe-area-inset-top)); margin: 24px 0 2px;
    padding: 0 16px; }
  .day { margin-top: 28px; }
  .grid { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  .card, .card .inner, .btn, .chip, .hint { transition: none; }
}
""".replace("@LIGHT@", _LIGHT_VARS)

_JS = """
(function () {
  var q = document.getElementById('q');
  var cards = [].slice.call(document.querySelectorAll('.card'));
  var days = [].slice.call(document.querySelectorAll('.day'));
  var empty = document.getElementById('empty');
  function filter() {
    var s = q.value.trim().toLowerCase();
    var any = false;
    cards.forEach(function (c) {
      c.classList.toggle('hide', !!s && c.getAttribute('data-s').indexOf(s) === -1);
    });
    days.forEach(function (d) {
      var has = d.querySelector('.card:not(.hide)');
      d.classList.toggle('hide', !has);
      if (has) any = true;
    });
    empty.querySelector('b').textContent = q.value.trim();
    empty.classList.toggle('hide', any || !s);
  }
  q.addEventListener('input', filter);
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== q) { e.preventDefault(); q.focus(); }
    else if (e.key === 'Escape' && document.activeElement === q) {
      q.value = ''; filter(); q.blur();
    }
  });
  function toggle(c) {
    var on = c.classList.toggle('flipped');
    c.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('a')) return;
    var c = e.target.closest('.card');
    if (c) toggle(c);
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList
        && e.target.classList.contains('card')) {
      e.preventDefault(); toggle(e.target);
    }
  });
  function rel(n) {
    if (n === 0) return 'today';
    if (n === 1) return 'tomorrow';
    if (n === -1) return 'yesterday';
    if (n < 0) return (-n) + ' days ago';
    if (n < 14) return 'in ' + n + ' days';
    if (n < 61) return 'in ' + Math.floor(n / 7) + ' weeks';
    return 'in ' + Math.round(n / 30.4) + ' months';
  }
  function relabel() {
    var now = new Date();
    var t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    days.forEach(function (d) {
      var p = d.getAttribute('data-date').split('-');
      var n = Math.round((new Date(+p[0], p[1] - 1, +p[2]) - t0) / 864e5);
      d.querySelector('.rel').textContent = rel(n);
      d.classList.toggle('today', n === 0);
      d.classList.toggle('past', n < 0);
    });
    todayBtn.classList.toggle('hide', !currentDay());
  }
  var todayBtn = document.getElementById('today');
  function currentDay() {
    return document.querySelector('.day.today') ||
      document.querySelector('.day:not(.past):not(.hide)');
  }
  todayBtn.addEventListener('click', function () {
    var t = currentDay();
    if (t) t.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  relabel();
  var first = currentDay();
  if (first && document.querySelector('.day.past')) {
    first.scrollIntoView({behavior: 'instant', block: 'start'});
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) relabel();
  });
  var root = document.documentElement;
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  document.getElementById('theme').addEventListener('click', function () {
    var cur = root.getAttribute('data-theme') || (mq.matches ? 'dark' : 'light');
    var next = cur === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try {
      if (next === (mq.matches ? 'dark' : 'light')) localStorage.removeItem('theme');
      else localStorage.setItem('theme', next);
    } catch (e) {}
  });
})();
"""

_SRC_LABEL = {"tvmaze": "TVmaze", "anilist": "AniList", "tmdb": "TMDB", "igdb": "IGDB"}

_PH_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'aria-hidden="true"><rect x="3" y="4.5" width="18" height="15" rx="3"/>'
    '<path d="m10.5 9.3 4.8 2.7-4.8 2.7z" fill="currentColor" stroke="none"/></svg>'
)

_HINT_SVG = (
    '<svg class="hint" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'
    '<path d="M12 11v5"/><circle cx="12" cy="7.6" r="0.6" fill="currentColor" '
    'stroke="none"/></svg>'
)

_SEARCH_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/></svg>'
)

_SUN_SVG = (
    '<svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4'
    'm11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"/></svg>'
)

_MOON_SVG = (
    '<svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
)

# Applies a saved manual theme before first paint so there is no flash.
_THEME_BOOT = (
    "try{var t=localStorage.getItem('theme');"
    "if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}"
)


def _card(r: Release) -> str:
    """One flip-card: poster + facts on the front, recap/description on the back."""
    hay = _xesc(" ".join(
        x for x in (r.title, r.detail, r.platform, _KIND_LABEL[r.kind]) if x
    ).lower())
    if r.image:
        poster = (f'<img class="poster" src="{_xesc(r.image)}" alt="" '
                  'loading="lazy" width="100" height="150">')
    else:
        poster = f'<span class="poster ph">{_PH_SVG}</span>'
    src = (f'<span class="src">{_xesc(r.platform)}</span><span class="sep">·</span>'
           if r.platform else "")
    meta = f'<p class="meta">{src}<span class="cat">{_KIND_LABEL[r.kind]}</span></p>'
    snippet = f'<p class="snippet">{_xesc(r.detail)}</p>' if r.detail else ""
    recap = f'<p class="recap">{_xesc(r.recap)}</p>' if r.recap else ""
    about = _xesc(r.about) if r.about else "No description available yet."
    more = (f'<a class="more" href="{_xesc(r.url)}" target="_blank" rel="noopener">'
            f"{_SRC_LABEL.get(r.source, 'Details')} ↗</a>") if r.url else ""
    return (
        f'<li class="card {r.kind}" data-s="{hay}" tabindex="0" role="button" '
        f'aria-expanded="false" aria-label="{_xesc(r.title)} — details">'
        '<div class="inner">'
        f'<div class="face front">{meta}'
        f'<h3 class="t">{_xesc(r.title)}</h3>{snippet}{poster}{_HINT_SVG}</div>'
        f'<div class="face back"><h3 class="t">{_xesc(r.title)}</h3>{recap}'
        f'<p class="about">{about}</p>{more}{_HINT_SVG}</div>'
        "</div></li>"
    )


def _wordmark(name: str) -> str:
    """scoutfeed-style two-tone lowercase wordmark: watch<feed> in accent."""
    low = name.lower()
    if low.endswith("feed") and len(low) > 4:
        return f'{_xesc(low[:-4])}<span class="tld">feed</span>'
    return _xesc(low)


def build_html(rels: list[Release], cal_name: str, public_url: str) -> str:
    today = date.today()
    base = public_url.rstrip("/") if public_url else ""
    ics_url = f"{base}/watch.ics" if base else "watch.ics"
    webcal = ics_url.replace("https://", "webcal://")

    upcoming = [r for r in rels if r.on >= today]
    counts: dict[str, int] = {}
    for r in upcoming:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    summary = " · ".join(
        f"{counts[k]} {_KIND_LABEL[k].lower()}"
        for k in ("tv", "anime", "movie", "game") if k in counts
    )

    rows: list[str] = []
    prev: date | None = None
    for r in sorted(rels, key=lambda x: (x.on, x.kind, x.title)):
        if r.on != prev:
            if prev is not None:
                rows.append("</ul>\n</section>")
            cls = " today" if r.on == today else (" past" if r.on < today else "")
            rows.append(
                f'<section class="day{cls}" data-date="{r.on:%Y-%m-%d}">\n'
                f"<h2>{r.on:%a, %b %-d, %Y}"
                f' <span class="rel">{_rel_label(r.on, today)}</span></h2>\n'
                '<ul class="grid">'
            )
            prev = r.on
        rows.append(_card(r))
    if prev is not None:
        rows.append("</ul>\n</section>")

    chips = "".join(
        f'<a class="chip" href="{f}">{label}</a>'
        for label, f in [("TV", "tv.ics"), ("Anime", "anime.ics"),
                         ("Films", "movies.ics"), ("Games", "games.ics"),
                         ("RSS", "watch.xml")]
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        f"<title>{_xesc(cal_name)} — upcoming releases</title>\n"
        '<link rel="icon" href="data:image/svg+xml,'
        '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22>'
        '<defs><linearGradient id=%22g%22 x1=%220%22 y1=%220%22 x2=%220%22 y2=%221%22>'
        '<stop offset=%220%22 stop-color=%22%237c5cf0%22/>'
        '<stop offset=%221%22 stop-color=%22%235436c9%22/></linearGradient></defs>'
        '<path d=%22M38 24 L24 5 M62 24 L76 5%22 stroke=%22%237c5cf0%22 '
        'stroke-width=%228%22 stroke-linecap=%22round%22 fill=%22none%22/>'
        '<rect x=%226%22 y=%2224%22 width=%2288%22 height=%2270%22 rx=%2216%22 '
        'fill=%22url(%23g)%22/>'
        '<rect x=%2217%22 y=%2235%22 width=%2254%22 height=%2248%22 rx=%229%22 '
        'fill=%22%23f3f1f8%22/>'
        '<circle cx=%2283%22 cy=%2246%22 r=%224.5%22 fill=%22%23f3f1f8%22 opacity=%22.9%22/>'
        '<circle cx=%2283%22 cy=%2262%22 r=%224.5%22 fill=%22%23f3f1f8%22 opacity=%22.9%22/>'
        '</svg>">\n'
        '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
        f"<script>{_THEME_BOOT}</script>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n<header>\n"
        '<p class="overline">Release calendar</p>\n'
        f"<h1>{_wordmark(cal_name)}</h1>\n"
        '<p class="tag">Every show, anime, film and game tracked — '
        "one auto-updating calendar.</p>\n"
        f'<p class="updated">Updated {today:%B %-d, %Y} · {len(upcoming)} upcoming'
        f" · {summary}</p>\n"
        f'<nav class="feeds" aria-label="Subscribe">'
        f'<a class="btn" href="{_xesc(webcal)}">Subscribe — everything</a>{chips}</nav>\n'
        f'<p class="url">Or add by URL: <code>{_xesc(ics_url)}</code></p>\n'
        "</header>\n<main>\n"
        f'<div class="search">{_SEARCH_SVG}'
        '<input id="q" type="search" placeholder="Search shows, films, games…" '
        'aria-label="Search releases" autocomplete="off" spellcheck="false">'
        "<kbd>/</kbd>"
        '<button id="today" type="button" aria-label="Jump to today">Today</button>'
        '<button id="theme" type="button" aria-label="Toggle light/dark theme">'
        f"{_SUN_SVG}{_MOON_SVG}</button></div>\n"
        '<div id="empty" class="hide" role="status">Nothing matches '
        "“<b></b>”.</div>\n"
        + "\n".join(rows) + "\n</main>\n<footer>\n"
        '<p>Tap a card for the story so far · Rebuilt daily · '
        '<a href="https://github.com/Amir-Hackett/watchfeed">source</a></p>\n'
        f"</footer>\n<script>{_JS}</script>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------- rss


def build_rss(rels: list[Release], title: str, link: str) -> str:
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    today = date.today()
    items = []
    for r in sorted(rels, key=lambda x: (x.on, x.title)):
        pub = datetime(r.on.year, r.on.month, r.on.day, tzinfo=timezone.utc)
        body = f"{_rel_label(r.on, today)} — {r.on:%A, %B %-d, %Y}"
        if r.platform:
            body += f" — {r.platform}"
        items.append(
            "    <item>\n"
            f"      <title>{_xesc(r.summary)}</title>\n"
            f"      <description>{_xesc(body)}</description>\n"
            f"      <pubDate>{pub:%a, %d %b %Y} 00:00:00 +0000</pubDate>\n"
            f"      <guid isPermaLink=\"false\">{r.uid}</guid>\n"
            f"      <category>{r.kind}</category>\n"
            + (f"      <link>{_xesc(r.url)}</link>\n" if r.url else "")
            + "    </item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n  <channel>\n'
        f"    <title>{_xesc(title)}</title>\n"
        f"    <link>{_xesc(link)}</link>\n"
        f"    <description>Upcoming TV, anime, film and game releases</description>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a watch/play calendar feed.")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--days", type=int, default=None, help="override horizon")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}", file=sys.stderr)
        return 1
    cfg = tomllib.loads(cfg_path.read_text())

    s = cfg.get("settings", {})
    horizon_days = args.days or s.get("horizon_days", 400)
    horizon = date.today() + timedelta(days=horizon_days)
    print(f"window: {_since()} -> {horizon} (page keeps {_PAST_DAYS}d history)\n")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def resilient(source: str, fetch) -> list[Release]:
        got = fetch()
        if got:
            _save_cache(out, source, got)
            return got
        cached = _load_cache(out, source)
        if cached:
            _warn(f"{source}: fetched nothing — reusing {len(cached)} events "
                  "from the last good run")
        return cached

    rels: list[Release] = []

    if tv := cfg.get("tv", {}).get("shows"):
        print("TV")
        rels += resilient("tvmaze", lambda: fetch_tvmaze(tv, horizon))

    if anime := cfg.get("anime", {}).get("shows"):
        print("\nANIME")
        rels += resilient("anilist", lambda: fetch_anilist(anime, horizon))

    if movies := cfg.get("movies", {}).get("titles"):
        key = os.environ.get("TMDB_API_KEY")
        print("\nMOVIES")
        if key:
            rels += resilient("tmdb", lambda: fetch_tmdb(movies, horizon, key))
        else:
            _warn("TMDB_API_KEY not set — skipping movies")

    if games := cfg.get("games", {}).get("titles"):
        cid = os.environ.get("TWITCH_CLIENT_ID")
        sec = os.environ.get("TWITCH_CLIENT_SECRET")
        print("\nGAMES")
        if cid and sec:
            rels += resilient("igdb", lambda: fetch_igdb(games, horizon, cid, sec))
        else:
            _warn("TWITCH_CLIENT_ID/SECRET not set — skipping games")

    if not rels:
        print("\nnothing found — not writing empty feeds", file=sys.stderr)
        return 2

    cal_name = s.get("calendar_name", "Watchfeed")
    alarm = s.get("alarm_minutes", 60)
    all_day = s.get("all_day", False)
    public_url = s.get("public_url", "")

    # The page shows a trailing week of history; feeds stay upcoming-only
    # (past alarms and negative RSS countdowns would be noise).
    upcoming = [r for r in rels if r.on >= date.today()]

    wrote = []
    (out / "watch.ics").write_text(build_ics(upcoming, cal_name, alarm, all_day), newline="")
    wrote.append("watch.ics")

    # Per-category feeds: subscribe separately, color separately, mute separately.
    per = [("tv", "tv.ics", "TV"), ("anime", "anime.ics", "Anime"),
           ("movie", "movies.ics", "Movies"), ("game", "games.ics", "Games")]
    for kind, fname, label in per:
        sub = [r for r in upcoming if r.kind == kind]
        (out / fname).write_text(
            build_ics(sub, f"{cal_name} {label}", alarm, all_day), newline="")
        wrote.append(fname)

    (out / "watch.xml").write_text(build_rss(upcoming, cal_name, public_url))
    wrote.append("watch.xml")
    (out / "index.html").write_text(build_html(rels, cal_name, public_url))
    wrote.append("index.html")

    by = {}
    for r in rels:
        by[r.kind] = by.get(r.kind, 0) + 1
    print(f"\n{len(rels)} events  " + "  ".join(f"{k}={v}" for k, v in sorted(by.items())))
    for f in wrote:
        print(f"wrote {out/f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
