# GoCary Transit Ops Dashboard

Polls GoCary's GTFS-RT feeds (vehicle positions, trip updates, service
alerts) and turns them into a live vehicle map, active alerts list, and
on-time performance rollup by route — hosted entirely on GitHub, no server
to run or pay for. Same shape as
[gocary-news-monitor](https://github.com/g0s0-85/gocary-news-monitor): a
GitHub Actions poller commits JSON under `docs/data/`, and a static
`docs/index.html` on GitHub Pages reads it.

## How it works

- **`scripts/poll_transit.py`** — fetches GoCary's three `.pb` feeds
  (`GTFS_VehiclePositions.pb`, `GTFS_TripUpdates.pb`,
  `GTFS_ServiceAlerts.pb`, all under `gocarylive.org/GTFS/Realtime/`),
  decodes them with `gtfs-realtime-bindings`, and writes:
  - `docs/data/live/vehicles.json` — current vehicle positions, each tagged
    on_time / early / late from its trip's next predicted stop.
  - `docs/data/live/alerts.json` — currently active service alerts.
  - `docs/data/live/trip_updates.json` — current per-trip delay snapshot.
  - `docs/data/performance/YYYY-MM-DD.json` — one file per service day
    (America/New_York), with on-time/early/late counts three ways:
    system-wide, per route, and **per stop** (each stop entry also breaks
    its counts down by the route(s) that serve it, since a stop can be
    shared). This file *is* the schedule-adherence history — see "On
    historical data" below.
  - `docs/data/routes.json` / `docs/data/stops.json` — route id → short
    name/color and stop id → name/lat/lon, refreshed weekly from GoCary's
    static GTFS zip (only needed for display; delay data comes straight
    from the RT feed).
  - `docs/data/status.json` — last poll time, error (if any), poll count.

  **On dedup:** TripUpdates repeats the same upcoming stop's predicted delay
  on every poll until the vehicle actually passes it, so counting every poll
  would count one real arrival dozens of times. The script instead tracks
  each `(trip_id, stop_sequence)` pair in `docs/data/live/pending_stops.json`
  with its most-recently-seen delay, and only rolls it into that day's
  performance file the moment it stops appearing in the feed — i.e. once,
  using the delay value closest to when the vehicle actually served that
  stop. A feed outage that empties TripUpdates will flush everything pending
  at once when it recovers; that's expected, not a bug.

  On-time is defined as delay between -60s (1 min early) and +300s (5 min
  late) — this is GoCary's own on-time standard, confirmed directly. See
  `EARLY_THRESHOLD_S` / `LATE_THRESHOLD_S` in the script to change it.

  **On stop coverage:** don't expect the unique-stop count to climb toward
  the full ~277-stop roster, or even the ~258 stops actually scheduled
  somewhere in the static GTFS. GoCary's RT feed only publishes
  StopTimeUpdate predictions for **timepoint stops** — the schedule-checkpoint
  subset of each route (confirmed directly: a Route 1 trip's static schedule
  has 27+ stops but only ~6 are flagged `timepoint=1` in `stop_times.txt`,
  and the live feed reports exactly that many, spanning the full route rather
  than clustering near the vehicle's position). Non-timepoint stops simply
  never appear in the feed, so once every active route has completed one
  full trip, the unique-stop count plateaus near (active routes) × (average
  timepoints per route) — around 54 for GoCary's ~9 routes as observed on
  2026-09-02 — and stays there. This is a structural property of the data
  source, not a bug to chase.

- **`.github/workflows/poll-transit.yml`** — runs the script and commits
  `docs/data` if anything changed. Only triggered by `workflow_dispatch`
  (see the external trigger below) — the same reasoning as the news monitor
  applies: GitHub's own `schedule:` trigger is unreliable and can race an
  external trigger landing at the same moment.
- **A [cron-job.org](https://cron-job.org) job** (set up separately, not
  part of this repo) calls GitHub's API on an interval to fire
  `workflow_dispatch`. This is what actually controls polling frequency.
  Every 1–2 minutes is reasonable; GoCary's feeds don't update faster than
  that.
- **`docs/index.html`** — a static dashboard (no backend): a live Leaflet
  map of vehicles color-coded by on-time status, active alerts, today's
  on-time performance by route, a filterable **by-stop** table (worst
  on-time% first, so problem stops surface without scrolling), and the last
  7 service days' system-wide trend. Reads data via the GitHub Contents API
  rather than fetching `data/*.json` directly, for the same CDN-caching
  reason documented in the news monitor (GitHub Pages caches for 10 minutes
  and ignores query strings; the Contents API caches for only 60 seconds and
  is used first, falling back to the Pages-served copy if that call fails,
  e.g. its 60-requests/hour unauthenticated rate limit).

## On historical data

There's no existing archive of GoCary's GTFS-RT feed anywhere — I checked
Transitland and Bus Observatory (the two major public GTFS-RT archives) and
neither has ever collected GoCary/Cary Transit; GTFS-RT is inherently
ephemeral (it only ever describes "right now"), so if nobody was polling it,
that history simply doesn't exist to retrieve. The `docs/data/performance/`
files this script writes, from the day you start running it forward, **are**
the historical archive — that's why the rollup is stop-level from day one
rather than just a route summary: whatever granularity isn't captured now is
gone for good. The dashboard's 7-day trend and by-stop table will fill in
day by day as `docs/data/performance/*.json` accumulates; there's nothing to
backfill.

## One-time setup

1. **Create a GitHub repo** and push this folder to it:
   ```bash
   git init
   git add .
   git commit -m "Set up GoCary transit ops dashboard"
   git branch -M main
   git remote add origin https://github.com/<you>/gocary-transit-dashboard.git
   git push -u origin main
   ```
   If you use a different GitHub username/org or repo name than
   `g0s0-85/gocary-transit-dashboard`, update the `REPO` constant near the
   top of the `<script>` block in `docs/index.html` to match — otherwise the
   dashboard will fetch a repo that doesn't exist and show empty states.

2. **Let the workflow push commits.**
   `Settings → Actions → General → Workflow permissions` → select
   **"Read and write permissions"** → Save.

3. **Turn on GitHub Pages.**
   `Settings → Pages` → **Source**: "Deploy from a branch", **Branch**:
   `main`, folder **`/docs`** → Save. GitHub gives you a URL like
   `https://<you>.github.io/gocary-transit-dashboard/`.

4. **Kick off the first poll**: Actions tab → "Poll GoCary Transit" → **Run
   workflow**. It'll take a few minutes of polling (and, if it's a weekday
   during service hours, a few stops actually being passed) before the
   on-time table has any data — the map and alerts should populate
   immediately if service is running.

5. **Set up the external trigger**: a fine-grained GitHub token scoped to
   this repo with "Actions: Read and write" permission, and a free
   cron-job.org job that POSTs to
   `https://api.github.com/repos/<you>/gocary-transit-dashboard/actions/workflows/poll-transit.yml/dispatches`
   with that token in an `Authorization: Bearer <token>` header and body
   `{"ref":"main"}`.

## Adjusting things

- **Poll frequency**: change the cron-job.org schedule (not the `cron:`
  line in the workflow — see above).
- **On-time thresholds**: `EARLY_THRESHOLD_S` / `LATE_THRESHOLD_S` in
  `scripts/poll_transit.py`.
- **Map center/zoom**: the `setView([lat, lon], zoom)` call in
  `docs/index.html`.
- **Route colors**: pulled from GoCary's static GTFS `route_color`; falls
  back to a default blue if a route doesn't set one.
