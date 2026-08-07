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
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


# ---------------------------------------------------------------- tvmaze


def fetch_tvmaze(titles: list[str], horizon: date) -> list[Release]:
    out: list[Release] = []
    today = date.today()
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

        net = (show.get("network") or show.get("webChannel") or {}).get("name", "")
        found = 0
        for ep in eps:
            if not ep.get("airdate"):
                continue
            d = date.fromisoformat(ep["airdate"])
            if not (today <= d <= horizon):
                continue
            when = None
            if ep.get("airstamp"):
                try:
                    when = datetime.fromisoformat(ep["airstamp"]).astimezone(timezone.utc)
                except ValueError:
                    pass
            label = f"S{ep['season']:02d}E{ep['number']:02d}" if ep.get("number") else "Special"
            if ep.get("name"):
                label += f" — {ep['name']}"
            out.append(Release(
                title=show["name"], detail=label, on=d, kind="tv", source="tvmaze",
                url=show.get("url", ""), platform=net, exact_time=when,
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
    airingSchedule(notYetAired: true, perPage: 50) {
      nodes { episode airingAt }
    }
    relations {
      edges { relationType node { id type } }
    }
"""

ANILIST_Q = f"query ($search: String) {{ Media(search: $search, type: ANIME) {{{_ANILIST_FIELDS}}} }}"
ANILIST_ID_Q = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{{_ANILIST_FIELDS}}} }}"


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
            found = 0
            for n in nodes:
                when = datetime.fromtimestamp(n["airingAt"], tz=timezone.utc)
                d = when.date()
                if not (today <= d <= horizon):
                    continue
                out.append(Release(
                    title=name, detail=f"Episode {n['episode']}", on=d, kind="anime",
                    source="anilist", url=m.get("siteUrl", ""), exact_time=when,
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

        # Prefer the soonest release that hasn't happened yet; else most popular.
        future = [h for h in hits if h.get("release_date") and h["release_date"] >= today.isoformat()]
        m = min(future, key=lambda h: h["release_date"]) if future else hits[0]

        if not m.get("release_date"):
            _warn(f"tmdb: {t!r} has no date set")
            continue
        d = date.fromisoformat(m["release_date"])
        if not (today <= d <= horizon):
            print(f"  tmdb    {m['title']}: 0 (date {d} outside window)")
            continue

        out.append(Release(
            title=m["title"], detail="Theatrical release", on=d, kind="movie",
            source="tmdb", url=f"https://www.themoviedb.org/movie/{m['id']}",
            platform="Theaters",
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
    lo, hi = int(time.mktime(today.timetuple())), int(time.mktime(horizon.timetuple()))

    for t in titles:
        safe = t.replace('"', '')
        body = (
            f'search "{safe}"; '
            f"fields name,url,release_dates.date,release_dates.human,"
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

        out.append(Release(
            title=g["name"], detail="Release day", on=d, kind="game", source="igdb",
            url=g.get("url", ""), platform=", ".join(plats),
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
    if n < 14:
        return f"in {n} days"
    if n < 61:
        return f"in {n // 7} weeks"
    return f"in {round(n / 30.4)} months"


_CSS = """
:root {
  --bg: #faf9f7; --surface: #ffffff; --text: #1c1917; --muted: #6b6560;
  --line: #e7e3de; --accent: #0f766e; --accent-fg: #ffffff;
  --tv-bg: #e0ecff; --tv-fg: #1d4ed8; --anime-bg: #ede9fe; --anime-fg: #6d28d9;
  --movie-bg: #fdeecd; --movie-fg: #92400e; --game-bg: #d9f2e5; --game-fg: #047857;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12100e; --surface: #1c1917; --text: #f5f5f4; --muted: #a8a29e;
    --line: #2b2724; --accent: #2dd4bf; --accent-fg: #042f2e;
    --tv-bg: #1c2f55; --tv-fg: #93c5fd; --anime-bg: #2e1f52; --anime-fg: #c4b5fd;
    --movie-bg: #3d2c12; --movie-fg: #fcd34d; --game-bg: #123529; --game-fg: #6ee7b7;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--text);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
header, main, footer { max-width: 40rem; margin: 0 auto; }
h1 { font-size: 32px; margin: 8px 0 4px; letter-spacing: -0.02em; }
.tag { margin: 0 0 4px; color: var(--muted); }
.updated { margin: 0 0 20px; font-size: 14px; color: var(--muted); }
.feeds { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.btn, .chip {
  display: inline-flex; align-items: center; min-height: 44px; padding: 8px 16px;
  border-radius: 10px; text-decoration: none; font-weight: 600; font-size: 15px;
}
.btn { background: var(--accent); color: var(--accent-fg); }
.chip { border: 1px solid var(--line); color: var(--text); background: var(--surface); }
.url { font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.url code { background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; padding: 3px 6px; }
a { color: var(--accent); }
a:focus-visible, .btn:focus-visible, .chip:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
h2 { font-size: 17px; margin: 28px 0 4px; padding-bottom: 6px;
  border-bottom: 1px solid var(--line); }
.rel { font-weight: 400; font-size: 14px; color: var(--muted); margin-left: 6px; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 10px;
  padding: 10px 0; border-bottom: 1px solid var(--line); }
li:last-child { border-bottom: 0; }
.badge { font-size: 12px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 999px; }
.badge.tv { background: var(--tv-bg); color: var(--tv-fg); }
.badge.anime { background: var(--anime-bg); color: var(--anime-fg); }
.badge.movie { background: var(--movie-bg); color: var(--movie-fg); }
.badge.game { background: var(--game-bg); color: var(--game-fg); }
.title { font-weight: 600; }
.title a { color: inherit; text-decoration: none; }
.title a:hover { text-decoration: underline; }
.detail, .meta { color: var(--muted); font-size: 14px; }
footer { margin-top: 40px; font-size: 14px; color: var(--muted); }
"""


def build_html(rels: list[Release], cal_name: str, public_url: str) -> str:
    today = date.today()
    base = public_url.rstrip("/") if public_url else ""
    ics_url = f"{base}/watch.ics" if base else "watch.ics"
    webcal = ics_url.replace("https://", "webcal://")

    counts: dict[str, int] = {}
    for r in rels:
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
                rows.append("</ul>")
            rows.append(
                f'<h2>{r.on:%a, %b %-d, %Y}'
                f' <span class="rel">{_rel_label(r.on, today)}</span></h2>\n<ul>'
            )
            prev = r.on
        title = f'<a href="{_xesc(r.url)}">{_xesc(r.title)}</a>' if r.url else _xesc(r.title)
        detail = f' <span class="detail">{_xesc(r.detail)}</span>' if r.detail else ""
        meta = f' <span class="meta">{_xesc(r.platform)}</span>' if r.platform else ""
        rows.append(
            f'<li><span class="badge {r.kind}">{_KIND_LABEL[r.kind]}</span>'
            f' <span class="title">{title}</span>{detail}{meta}</li>'
        )
    if prev is not None:
        rows.append("</ul>")

    chips = "".join(
        f'<a class="chip" href="{f}">{label}</a>'
        for label, f in [("TV", "tv.ics"), ("Anime", "anime.ics"),
                         ("Films", "movies.ics"), ("Games", "games.ics"),
                         ("RSS", "watch.xml")]
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_xesc(cal_name)} — upcoming releases</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n<header>\n"
        f"<h1>{_xesc(cal_name)}</h1>\n"
        '<p class="tag">Every show, anime, film and game tracked — '
        "one auto-updating calendar.</p>\n"
        f'<p class="updated">Updated {today:%B %-d, %Y} · {len(rels)} upcoming'
        f" · {summary}</p>\n"
        f'<nav class="feeds" aria-label="Subscribe">'
        f'<a class="btn" href="{_xesc(webcal)}">Subscribe — everything</a>{chips}</nav>\n'
        f'<p class="url">Or add by URL: <code>{_xesc(ics_url)}</code></p>\n'
        "</header>\n<main>\n" + "\n".join(rows) + "\n</main>\n<footer>\n"
        '<p>Rebuilt daily · <a href="https://github.com/Amir-Hackett/watchfeed">'
        "source</a></p>\n</footer>\n</body>\n</html>\n"
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
    print(f"window: {date.today()} -> {horizon}\n")

    rels: list[Release] = []

    if tv := cfg.get("tv", {}).get("shows"):
        print("TV")
        rels += fetch_tvmaze(tv, horizon)

    if anime := cfg.get("anime", {}).get("shows"):
        print("\nANIME")
        rels += fetch_anilist(anime, horizon)

    if movies := cfg.get("movies", {}).get("titles"):
        key = os.environ.get("TMDB_API_KEY")
        print("\nMOVIES")
        if key:
            rels += fetch_tmdb(movies, horizon, key)
        else:
            _warn("TMDB_API_KEY not set — skipping movies")

    if games := cfg.get("games", {}).get("titles"):
        cid = os.environ.get("TWITCH_CLIENT_ID")
        sec = os.environ.get("TWITCH_CLIENT_SECRET")
        print("\nGAMES")
        if cid and sec:
            rels += fetch_igdb(games, horizon, cid, sec)
        else:
            _warn("TWITCH_CLIENT_ID/SECRET not set — skipping games")

    if not rels:
        print("\nnothing found — not writing empty feeds", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cal_name = s.get("calendar_name", "Watchfeed")
    alarm = s.get("alarm_minutes", 60)
    all_day = s.get("all_day", False)
    public_url = s.get("public_url", "")

    wrote = []
    (out / "watch.ics").write_text(build_ics(rels, cal_name, alarm, all_day), newline="")
    wrote.append("watch.ics")

    # Per-category feeds: subscribe separately, color separately, mute separately.
    per = [("tv", "tv.ics", "TV"), ("anime", "anime.ics", "Anime"),
           ("movie", "movies.ics", "Movies"), ("game", "games.ics", "Games")]
    for kind, fname, label in per:
        sub = [r for r in rels if r.kind == kind]
        (out / fname).write_text(
            build_ics(sub, f"{cal_name} {label}", alarm, all_day), newline="")
        wrote.append(fname)

    (out / "watch.xml").write_text(build_rss(rels, cal_name, public_url))
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
