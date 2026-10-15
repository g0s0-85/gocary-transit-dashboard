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
    name/color and stop id → name/lat/lon, built from GoCary's static GTFS
    feed at **`https://gocary.org/_GTFS`** (the URL GoCary's own
    developer-resources listing gives; it redirects to whichever zip GoCary
    last uploaded, so it stays correct across uploads). Only needed for
    display; delay data comes straight from the RT feed. The poller asks
    hourly (a `Last-Modified` HEAD request) whether GoCary published a new
    file and rebuilds only then — plus a weekly safety rebuild — so a
    detour or special-event feed shows up within the hour. `stops.json` is
    *merged* across feeds rather than replaced, so a stop dropped by a
    short-term feed keeps its map dot for the history that references it.
    Source history: Trillium mirror (2023-12-18; missing routes 2/9 and
    stops with live activity) → `gocarylive.org/GTFS/google_transit.zip`
    (last modified 2026-01-23; missed the Oct 2026 detour that Google and
    Transit app already showed) → `gocary.org/_GTFS`. This feed's
    `route_id` equals the short display code the RT feed uses, and
    `stop_id == stop_code`.
  - `docs/data/trip_last_stops.json` — trip id → highest stop_sequence in
    that trip's static pattern, from the same static feed and refreshed on
    the same schedule as routes.json/stops.json. Meant to exclude a trip's
    final stop from the on-time rollup — **but currently has no effect**; see
    "On matching TripSpark's own OTP numbers" below.
  - `docs/data/route_shapes.json` — each route's drawn alignment (de-duplicated
    `shapes.txt` polylines grouped by route via `trips.txt`, with the route's
    name and color), from the same static feed and refresh schedule. Powers
    the route lines on both maps. Note GoCary's `shape_pt_sequence` values
    aren't contiguous or in file order, so points are sorted per shape before
    drawing. These are the *published schedule's* alignments — GTFS-RT carries
    no route geometry at all (verified: the feeds only contain trip updates,
    vehicle positions, and alerts), so a line is only as current as the last
    static feed GoCary uploaded; a detour appears once a feed describing it
    is published. (This is why the maps once showed the retired Downtown Loop
    and Route 2's old alignment: they were read from a January file.)
    If building shapes ever fails, an empty file with an `error` field is
    written instead so the poller doesn't re-download the feed every minute.
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
  the full stop roster in `docs/data/stops.json`. GoCary's RT feed only
  publishes StopTimeUpdate predictions for **timepoint stops** — the
  schedule-checkpoint subset of each route (confirmed directly: a Route 1
  trip's static schedule has 27+ stops but only ~6 are flagged
  `timepoint=1` in `stop_times.txt`, and the live feed reports exactly
  that many, spanning the full route rather than clustering near the
  vehicle's position). Non-timepoint stops simply never appear in the
  feed, so once every active route has completed one full trip, the
  unique-stop count plateaus near (active routes) × (average timepoints
  per route) and stays there — this is a structural property of the data
  source, not a bug to chase. (On 2026-09-02, with the then-current static
  feed, that was ~54 unique stops across ~9 routes; expect a different
  number now that routes "2"/"9" and their timepoints are actually counted
  too.)

  **On matching TripSpark's own OTP numbers:** our system-wide on-time %
  runs a few points below GoCary's official TripSpark "Yesterday
  Departure-OTP" report (e.g. 81.2% vs. 87.2% on 2026-09-04). Two things
  contribute:
  1. **Attempted, but not working yet** — TripSpark's report filters
     explicitly exclude a trip's final stop (`Is Last Stop in Trip? Equal to
     0`), since a bus doesn't meaningfully "depart" from where its trip
     ends. We count it anyway (falling back to arrival delay when a final
     stop has no departure). `process_trip_updates` has code to drop it
     using `trip_last_stops.json`, which maps trip_id → last stop_sequence
     — but that join never matches: the live RT feed's `trip_id`s appear in
     neither published static feed (0 of 80 live trips found in the January
     file or the Oct 14 one; the two static files share none with each other
     either). An early version of this note claimed the IDs matched; that
     was only ever checked by *format* (both are UUIDs), never by value. The
     lookup fails open, so nothing is wrongly dropped — and nothing is
     excluded, so `eol_stops_excluded` in status.json stays 0. Making it
     work needs a different join key (e.g. route + direction + scheduled
     start time) or trip IDs that line up between TripSpark's RT and static
     outputs.
  2. **Not fixed, and can't be from the public feed alone** — our daily
     sample size runs 2-3x TripSpark's for the same day, and the ratio
     varies by route (1.78x-3.47x checked on 2026-09-04), which rules out
     a single missing filter as the explanation. TripSpark's OTP module
     evidently uses a narrower, curated set of "official" timepoints per
     route than what GTFS-RT happens to expose as `timepoint=1`. Pinning
     this down exactly would need TripSpark's authoritative timepoint list
     per route (via `FRWebAPI`/`StreetsWebServiceV2` direct access, not
     the public RT feed) rather than inferring it from what gets published.
     (Any fix here would only apply to stops finalized after it ships —
     past days keep whatever their original counts were, since the
     per-event stop_sequence isn't retained once rolled up.)

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
