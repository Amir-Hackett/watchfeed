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
            # 429 = rate limited. Back off and retry; anything else is fatal.
            if e.code == 429 and attempt < 2:
                time.sleep(2 ** attempt * 3)
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

ANILIST_Q = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
    siteUrl
    status
    airingSchedule(notYetAired: true, perPage: 50) {
      nodes { episode airingAt }
    }
  }
}
"""


def fetch_anilist(titles: list[str], horizon: date) -> list[Release]:
    out: list[Release] = []
    today = date.today()
    for t in titles:
        try:
            body = json.dumps({"query": ANILIST_Q, "variables": {"search": t}}).encode()
            resp = _get("https://graphql.anilist.co", data=body)
        except Exception as e:
            _warn(f"anilist: lookup failed for {t!r} ({e})")
            continue

        m = (resp.get("data") or {}).get("Media")
        if not m:
            _warn(f"anilist: no match for {t!r}")
            continue

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
        if not nodes and m.get("status") == "NOT_YET_RELEASED":
            _warn(f"anilist: {name} announced but no schedule yet")
        print(f"  anilist {name}: {found}")
        time.sleep(0.8)  # AniList: 90 req/min. Be polite.
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


# ---------------------------------------------------------------- rss


def _xesc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def build_rss(rels: list[Release], title: str, link: str) -> str:
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for r in sorted(rels, key=lambda x: (x.on, x.title)):
        pub = datetime(r.on.year, r.on.month, r.on.day, tzinfo=timezone.utc)
        body = f"{r.on:%A, %B %-d, %Y}"
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

    ics = build_ics(
        rels,
        s.get("calendar_name", "Watchfeed"),
        s.get("alarm_minutes", 60),
        s.get("all_day", False),
    )
    rss = build_rss(rels, s.get("calendar_name", "Watchfeed"), s.get("public_url", ""))

    (out / "watch.ics").write_text(ics, newline="")
    (out / "watch.xml").write_text(rss)

    by = {}
    for r in rels:
        by[r.kind] = by.get(r.kind, 0) + 1
    print(f"\n{len(rels)} events  " + "  ".join(f"{k}={v}" for k, v in sorted(by.items())))
    print(f"wrote {out/'watch.ics'}")
    print(f"wrote {out/'watch.xml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
