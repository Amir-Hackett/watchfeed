# watchfeed

One `.ics` and one `.rss` for every show, anime, film and game you track.
Subscribe once on your iPhone; it updates itself.

**Landing page:** <https://amir-hackett.github.io/watchfeed/> — everything
upcoming, grouped by date, with subscribe links for each feed.

Live feeds (rebuilt daily at 9am UTC by GitHub Actions, served from `/docs`
via Pages):

- `https://amir-hackett.github.io/watchfeed/watch.ics` — everything
- `https://amir-hackett.github.io/watchfeed/tv.ics` — plus `anime.ics`,
  `movies.ics`, `games.ics` at the same base URL
- `watch.xml` — RSS countdowns

No dependencies. Python 3.11+ (uses stdlib `tomllib`).

```
python3 watchfeed.py --config config.toml --out ./out
```

Outputs:

- `watch.ics` — everything, one calendar
- `tv.ics`, `anime.ics`, `movies.ics`, `games.ics` — per-category calendars
  (subscribe separately to color or mute a category on its own)
- `watch.xml` — RSS with "in N days" countdowns
- `index.html` — a static landing page: poster cards grouped by date with
  live search (`/` to focus), and each card flips to a description, a
  "previously aired" recap for TV, and a link to the source page

---

## Sources

| Source | Covers | Key needed | Granularity |
|---|---|---|---|
| TVmaze | TV | none | per episode |
| AniList | anime | none | per episode |
| TMDB | movies | `TMDB_API_KEY` | release day |
| IGDB | games | `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` | release day |

Missing keys skip that source with a warning rather than failing the run.

### Getting the keys

**TMDB** — themoviedb.org → account → Settings → API → request a key.
Free, approved instantly.

**IGDB** — IGDB runs on Twitch auth. dev.twitch.tv/console → Register Your
Application → grab Client ID and generate a secret. The tool exchanges them
for a bearer token on each run.

```bash
export TMDB_API_KEY=...
export TWITCH_CLIENT_ID=...
export TWITCH_CLIENT_SECRET=...
```

---

## Config

`config.toml` is the only thing you edit. Add a title to the right list and
it shows up on the next run.

```toml
[settings]
horizon_days  = 400    # how far ahead to look
alarm_minutes = 60     # popup lead time. 0 = silent calendar
all_day       = false  # true = ignore air times, use all-day blocks
```

`all_day = true` is worth trying if timed events clutter your day view.
Anime especially — AniList gives you Japanese broadcast times, which land at
strange hours in Eastern.

---

## Hosting it

The `.ics` has to live at a URL your phone can reach. Cheapest paths:

**GitHub Pages + Actions** — free, no server.

`.github/workflows/build.yml`:

```yaml
name: watchfeed
on:
  schedule: [{ cron: "0 9 * * *" }]   # 9am UTC daily
  workflow_dispatch:
permissions:
  contents: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python3 watchfeed.py --config config.toml --out ./docs
        env:
          TMDB_API_KEY: ${{ secrets.TMDB_API_KEY }}
          TWITCH_CLIENT_ID: ${{ secrets.TWITCH_CLIENT_ID }}
          TWITCH_CLIENT_SECRET: ${{ secrets.TWITCH_CLIENT_SECRET }}
      - run: |
          git config user.name github-actions
          git config user.email github-actions@github.com
          git add docs && git diff --staged --quiet || git commit -m "rebuild feed"
          git push
```

Enable Pages on `/docs`. Feed lands at
`https://<you>.github.io/<repo>/watch.ics`.

**Vercel** — you already have it connected. Drop the outputs in `/public`
and point a cron job at a rebuild route.

---

## Subscribing on a Mac

Don't click `webcal://` links in a browser — whatever app owns the scheme
gets them (Chrome, if it's your default, opens a dead tab). Either use
Calendar's File → New Calendar Subscription, or force it from a shell:

```
open -a Calendar "webcal://amir-hackett.github.io/watchfeed/tv.ics"
```

Subscribe to the per-category feeds separately so each gets its own color.
In the settings sheet after subscribing:

- **Color** — pick per category; "Other…" takes a custom hex
- **Location: iCloud** — syncs the subscription to iPhone/iPad too, so you
  can skip the iPhone steps below
- **Auto-refresh: Every hour** — the feed's own TTL is 6h; hourly polling
  picks rebuilds up promptly
- **Remove: Alerts — uncheck it** — it's on by default and strips the
  `alarm_minutes` reminders the feed ships with

---

## Subscribing on iPhone

Settings → Calendar → Accounts → Add Account → Other → **Add Subscribed
Calendar** → paste the URL.

Do it this way, not by tapping the link in Safari. Tapping imports a static
snapshot; subscribing keeps it live.

Then Settings → Calendar → Accounts → your feed → **Fetch New Data** and set
it to hourly. iOS defaults subscribed calendars to a lazy refresh.

The feed advertises `REFRESH-INTERVAL` and `X-PUBLISHED-TTL` of 6 hours,
which Apple Calendar on macOS honors and iOS mostly ignores. Set the fetch
interval manually.

---

## Design notes

**Stable UIDs.** Each event's UID is a hash of source + title + episode +
date. Same event across runs = same UID, so your calendar updates in place
instead of accumulating duplicates. Change the date of an episode upstream
and you get a new event plus an orphan — the orphan disappears on the next
full fetch because the feed is regenerated whole, not patched.

**Empty runs abort.** If every source fails, the tool exits non-zero without
writing. Otherwise a transient network failure would blank your calendar.

**All-day DTEND is exclusive.** Per RFC 5545, an all-day event on Aug 2 ends
Aug 3. Getting this wrong is the classic reason events render across two days.

**Line folding is byte-based.** ICS caps lines at 75 octets, not characters.
Anime titles with non-ASCII break naive character-based folding.

**Rate limits.** AniList allows 90 req/min, IGDB 4 req/sec. Both are throttled
in code. TVmaze asks for roughly 20 calls per 10 seconds and isn't throttled —
if your TV list runs long, add a sleep in `fetch_tvmaze`.

---

## Known gaps

- **TVmaze matches by search**, so generic titles can grab the wrong show.
  `"Furious"` and `"Fable"` are the risky kind. If a title resolves wrong,
  swap it for the TVmaze show ID and use `/shows/{id}` directly.
- **Movies with no confirmed date** are dropped rather than guessed. TMDB
  often carries a placeholder Jan 1 date for unscheduled films; those get
  filtered by the window check but not always.
- **Games marked TBA** in IGDB produce a warning and nothing else. Most of
  the 2027 slate is in this state.
- **Anime seasons** are separate AniList entries, but the tool follows
  SEQUEL relations automatically — `"Blue Box"` also tracks Blue Box
  Season 2 without a config change. Spin-offs and side stories are not
  sequels; add those explicitly.
