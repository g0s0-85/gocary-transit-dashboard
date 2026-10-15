"""
Poll GoCary's GTFS-RT feeds (VehiclePositions, TripUpdates, ServiceAlerts),
update the live snapshots under docs/data/live/, and roll finalized stop-time
predictions up into a per-service-day on-time performance file under
docs/data/performance/. Meant to be run on a schedule by
.github/workflows/poll-transit.yml, which commits whatever this script writes
under docs/data/.

Why "finalized" and not "every poll": TripUpdates repeats the same upcoming
stop's predicted delay on every poll until the vehicle passes it (or the
schedule_relationship changes), so counting it on every poll would count one
real event dozens of times. Instead each (trip_id, stop_sequence) pair is
tracked in docs/data/live/pending_stops.json with its most-recently-seen
delay; the moment a pending pair stops showing up in the feed, its last-known
delay (closest to the vehicle's actual arrival/departure) is the one counted
into that day's rollup, then the pair is dropped from pending. This also
means a mid-outage gap that empties the feed will flush everything pending at
once when the feed comes back -- expected, not a bug.
"""

import csv
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

VEHICLE_POSITIONS_URL = "https://www.gocarylive.org/GTFS/Realtime/GTFS_VehiclePositions.pb"
TRIP_UPDATES_URL = "https://www.gocarylive.org/GTFS/Realtime/GTFS_TripUpdates.pb"
SERVICE_ALERTS_URL = "https://www.gocarylive.org/GTFS/Realtime/GTFS_ServiceAlerts.pb"
# GoCary's static feed, at the URL their own developer-resources listing
# gives (https://gocary.org/_GTFS, a redirect to a WordPress upload that
# GoCary replaces when they publish a new feed -- so this stays correct
# across uploads). Found 2026-10-15 after Google/Transit app showed a
# detour this dashboard didn't: gocarylive.org/GTFS/google_transit.zip, which
# this used to read, had last been modified 2026-01-23, while this URL had
# been updated the day before (a short-term "NC State Fair" feed).
# History: first the Trillium mirror (2023-12-18; missing routes 2/9 and
# stops with live activity), then gocarylive.org (Jan 2026), now this.
# route_id equals the short display code the RT feed uses, and
# stop_id == stop_code, so the parsing below is unchanged.
STATIC_GTFS_URL = "https://gocary.org/_GTFS"

SERVICE_TZ = ZoneInfo("America/New_York")

# A stop-time prediction counts as "on time" if its delay (seconds, GTFS-RT
# StopTimeEvent.delay: positive = late, negative = early) falls in this
# window. Outside it, it's bucketed as early or late. This is GoCary's own
# on-time definition (confirmed directly, not just a generic default): more
# than 1 minute ahead of schedule is early, more than 5 minutes behind is
# late.
EARLY_THRESHOLD_S = -60
LATE_THRESHOLD_S = 300

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LIVE_DIR = DATA_DIR / "live"
PERF_DIR = DATA_DIR / "performance"
ROUTES_FILE = DATA_DIR / "routes.json"
STOPS_FILE = DATA_DIR / "stops.json"
TRIP_LAST_STOPS_FILE = DATA_DIR / "trip_last_stops.json"
ROUTE_SHAPES_FILE = DATA_DIR / "route_shapes.json"
STATUS_FILE = DATA_DIR / "status.json"
PENDING_FILE = LIVE_DIR / "pending_stops.json"

# Safety net: rebuild from the feed at least this often even if we can't tell
# whether it changed (e.g. the server stops sending Last-Modified).
ROUTES_MAX_AGE_S = 7 * 24 * 3600
# How often to ask whether GoCary has published a newer static feed. Detours
# and special-event feeds can appear (and expire) within days, so a weekly
# refresh would leave the maps showing the wrong routing for most of one.
STATIC_CHECK_INTERVAL_S = 3600


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def service_date():
    return datetime.now(SERVICE_TZ).date().isoformat()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    # compact=True for coordinate-heavy files, where indent=2 would put every
    # lat and lon on its own line and balloon the file ~5x for no benefit.
    text = json.dumps(data, separators=(",", ":")) if compact else json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")


def fetch_feed(url):
    resp = requests.get(url, timeout=20, headers={"User-Agent": "gocary-transit-dashboard/1.0 (+github actions)"})
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def build_route_shapes(zf, routes):
    """route_id -> {short_name, long_name, color, shapes: [[[lat, lon], ...]]}
    for drawing each route's alignment on the dashboard maps.

    trips.txt says which shape_ids each route's trips follow (a route has
    several -- directions and pattern variants, 4-6 each in GoCary's feed);
    shapes.txt holds the points. shape_pt_sequence is NOT contiguous or in
    file order in GoCary's feed (first rows are sequence 9, then 26, ...),
    so points must be sorted by it before drawing or the line zigzags.
    Coordinates rounded to 5 decimals (~1 m) to keep the file small."""
    shape_routes = {}
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("shape_id"):
                shape_routes.setdefault(row["shape_id"], set()).add(row["route_id"])

    points_by_shape = {}
    with zf.open("shapes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            shape_id = row["shape_id"]
            if shape_id not in shape_routes:
                continue
            points_by_shape.setdefault(shape_id, []).append(
                (int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
            )

    out = {}
    for shape_id in sorted(points_by_shape):
        line = []
        for _, lat, lon in sorted(points_by_shape[shape_id]):
            point = [round(lat, 5), round(lon, 5)]
            if not line or line[-1] != point:
                line.append(point)
        for route_id in sorted(shape_routes[shape_id]):
            meta = routes.get(route_id, {})
            entry = out.setdefault(route_id, {
                "short_name": meta.get("short_name", route_id),
                "long_name": meta.get("long_name", ""),
                "color": meta.get("color", "2563eb"),
                "shapes": [],
            })
            # Several shape_ids can trace the same path; don't draw it twice.
            if line not in entry["shapes"]:
                entry["shapes"].append(line)
    return out


def _age_s(iso_ts):
    return time.time() - datetime.fromisoformat(iso_ts).timestamp()


def static_feed_changed(existing_routes):
    """True if GoCary's published static feed differs from the one
    existing_routes (routes.json) was built from, judged by its
    Last-Modified header (a HEAD request, so no download unless something
    actually changed).
    - Key never recorded (file predates this tracking): True, so one rebuild
      starts tracking it.
    - Recorded as empty (server sends no Last-Modified): False; we can't
      detect changes, so only the weekly rebuild applies -- otherwise
      "unknown" would read as "changed" and re-download every interval.
    - Check itself fails: False; keep what we have and retry next interval
      rather than treating a flaky server as a new feed."""
    if "_source_last_modified" not in existing_routes:
        return True
    known_last_modified = existing_routes["_source_last_modified"]
    if not known_last_modified:
        return False
    try:
        head = requests.head(STATIC_GTFS_URL, timeout=20, allow_redirects=True)
        head.raise_for_status()
    except Exception as exc:
        print(f"Warning: couldn't check the static feed for changes ({exc}); keeping current copy")
        return False
    current = head.headers.get("Last-Modified")
    return bool(current) and current != known_last_modified


def ensure_static_gtfs():
    """Refresh docs/data/routes.json, stops.json, trip_last_stops.json, and
    route_shapes.json from GoCary's static GTFS.

    Checked at most once per STATIC_CHECK_INTERVAL_S: a cheap Last-Modified
    probe decides whether GoCary has published something new, and only then
    is the feed downloaded and rebuilt (also rebuilt if it's been a week
    regardless, or if any derived file is missing -- so adding a new derived
    file here doesn't require manually deleting the cached ones). If a
    refresh fails we keep whatever we already had rather than failing the
    whole run, and note the attempt so a blocked or flaky source gets
    retried hourly instead of every poll."""
    existing_routes = load_json(ROUTES_FILE, None)
    existing_stops = load_json(STOPS_FILE, None)
    existing_trip_last_stops = load_json(TRIP_LAST_STOPS_FILE, None)
    all_cached = all(p.exists() for p in (ROUTES_FILE, STOPS_FILE, TRIP_LAST_STOPS_FILE, ROUTE_SHAPES_FILE))
    if existing_routes is not None and all_cached:
        fetched_at = existing_routes.get("_fetched_at")
        checked_at = existing_routes.get("_checked_at") or fetched_at
        if fetched_at and checked_at and _age_s(fetched_at) < ROUTES_MAX_AGE_S:
            cached = (existing_routes, (existing_stops or {}), (existing_trip_last_stops or {}))
            if _age_s(checked_at) < STATIC_CHECK_INTERVAL_S:
                return cached
            if not static_feed_changed(existing_routes):
                existing_routes["_checked_at"] = now_iso()
                save_json(ROUTES_FILE, existing_routes)
                return cached

    try:
        resp = requests.get(STATIC_GTFS_URL, timeout=60)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))

        fetched_at = now_iso()
        routes = {
            "_fetched_at": fetched_at,
            "_checked_at": fetched_at,
            "_source_last_modified": resp.headers.get("Last-Modified"),
        }
        with zf.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                routes[row["route_id"]] = {
                    "short_name": row.get("route_short_name") or row.get("route_long_name") or row["route_id"],
                    "long_name": row.get("route_long_name") or "",
                    "color": row.get("route_color") or "2563eb",
                }

        # Start from the stops we already know and let the new feed overwrite
        # them, rather than replacing the list: a short-term feed (the 2026
        # State Fair one) can drop stops that still have rollup history (stop
        # 101 did), and a stop missing from stops.json silently loses its dot
        # on the adherence map. Cost: a genuinely retired stop lingers in
        # stops.json (only visible under "show all stops").
        stops = {k: v for k, v in (existing_stops or {}).items() if k != "_fetched_at"}
        stops["_fetched_at"] = fetched_at
        with zf.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                # GoCary's RT feed identifies stops by stop_code (a short
                # rider-facing number, e.g. "9322"), not the static GTFS's
                # internal stop_id (e.g. "778286") -- confirmed by
                # cross-checking a stop_id showing up unresolved in the RT
                # feed against this file. Key on stop_code so StopTimeUpdate
                # lookups actually resolve to a name; fall back to stop_id
                # for the rare row missing a stop_code.
                key = row.get("stop_code") or row["stop_id"]
                stops[key] = {
                    "name": row.get("stop_name") or key,
                    "lat": float(row["stop_lat"]) if row.get("stop_lat") else None,
                    "lon": float(row["stop_lon"]) if row.get("stop_lon") else None,
                }

        # trip_id -> highest stop_sequence in that trip's static pattern, so
        # process_trip_updates can exclude a trip's final stop from the
        # on-time rollup -- matching TripSpark's own OTP methodology
        # ("Is Last Stop in Trip? Equal to 0"), confirmed directly from
        # their emailed report filters.
        # KNOWN LIMITATION (verified 2026-10-15): this does nothing today.
        # Only the *format* of trip_id (a UUID) was ever checked against the
        # RT feed, never the values -- and 0 of 80 live RT trip_ids appear in
        # either published static feed (nor do the two static feeds share any
        # with each other). The lookup fails open, so nothing is wrongly
        # dropped, but nothing is excluded either. Needs a different join key.
        trip_last_stops = {"_fetched_at": fetched_at}
        max_seq_by_trip = {}
        with zf.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                trip_id = row["trip_id"]
                seq = int(row["stop_sequence"])
                if seq > max_seq_by_trip.get(trip_id, -1):
                    max_seq_by_trip[trip_id] = seq
        trip_last_stops.update(max_seq_by_trip)

        # Isolated so a problem drawing routes can never block the data the
        # OTP rollup actually depends on. On failure an empty marker file is
        # still written (with the error) -- otherwise its absence would make
        # every poll re-download and re-parse the whole feed trying again.
        try:
            route_shapes = {"_fetched_at": fetched_at, "routes": build_route_shapes(zf, routes)}
        except Exception as exc:
            print(f"Warning: failed to build route shapes ({exc})")
            route_shapes = {"_fetched_at": fetched_at, "error": str(exc), "routes": {}}

        save_json(ROUTES_FILE, routes)
        save_json(STOPS_FILE, stops)
        save_json(TRIP_LAST_STOPS_FILE, trip_last_stops)
        save_json(ROUTE_SHAPES_FILE, route_shapes, compact=True)
        return routes, stops, trip_last_stops
    except Exception as exc:
        print(f"Warning: failed to refresh static GTFS files ({exc}); keeping existing copy")
        if existing_routes is not None:
            # Record the attempt so a blocked or flaky source is retried at
            # the next interval, not on every one-minute poll.
            existing_routes["_checked_at"] = now_iso()
            save_json(ROUTES_FILE, existing_routes)
        return (
            existing_routes or {"_fetched_at": None},
            existing_stops or {"_fetched_at": None},
            existing_trip_last_stops or {"_fetched_at": None},
        )


def classify(delay_s):
    if delay_s < EARLY_THRESHOLD_S:
        return "early"
    if delay_s > LATE_THRESHOLD_S:
        return "late"
    return "on_time"


def stop_delay(stop_time_update):
    """Prefer departure delay (reflects the vehicle actually having served
    the stop) and fall back to arrival delay."""
    if stop_time_update.HasField("departure") and stop_time_update.departure.HasField("delay"):
        return stop_time_update.departure.delay
    if stop_time_update.HasField("arrival") and stop_time_update.arrival.HasField("delay"):
        return stop_time_update.arrival.delay
    return None


def process_trip_updates(feed, routes, stops, trip_last_stops):
    pending = load_json(PENDING_FILE, {})
    current_keys = set()
    live_trip_summaries = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        route_id = tu.trip.route_id

        trip_pending = []
        for stu in tu.stop_time_update:
            delay = stop_delay(stu)
            if delay is None:
                continue
            key = f"{trip_id}:{stu.stop_sequence}"
            current_keys.add(key)
            pending[key] = {
                "route_id": route_id,
                "trip_id": trip_id,
                "stop_sequence": stu.stop_sequence,
                "stop_id": stu.stop_id if stu.HasField("stop_id") else None,
                "delay": delay,
            }
            trip_pending.append((stu.stop_sequence, delay))

        if trip_pending:
            trip_pending.sort(key=lambda t: t[0])
            next_stop_seq, next_delay = trip_pending[0]
            live_trip_summaries.append({
                "trip_id": trip_id,
                "route_id": route_id,
                "route_short_name": routes.get(route_id, {}).get("short_name", route_id),
                "next_stop_sequence": next_stop_seq,
                "delay_seconds": next_delay,
                "status": classify(next_delay),
            })

    finalized_raw = [v for k, v in pending.items() if k not in current_keys]
    pending = {k: v for k, v in pending.items() if k in current_keys}
    save_json(PENDING_FILE, pending)
    save_json(LIVE_DIR / "trip_updates.json", {
        "updated_at": now_iso(),
        "trips": live_trip_summaries,
    })

    # Exclude each trip's final stop from the OTP rollup, matching
    # TripSpark's own methodology: a bus doesn't meaningfully "depart" from
    # where its trip ends, so their report filters those out entirely. If a
    # trip_id isn't in the static lookup (e.g. an extra/unscheduled trip, or
    # the static feed hasn't loaded yet), fail open and count it rather than
    # risk dropping a legitimate stop.
    finalized = []
    eol_excluded = 0
    for entry in finalized_raw:
        last_seq = trip_last_stops.get(entry["trip_id"])
        if last_seq is not None and entry["stop_sequence"] == last_seq:
            eol_excluded += 1
            continue
        finalized.append(entry)

    if finalized:
        apply_rollup(finalized, routes, stops)

    trip_delay_by_id = {t["trip_id"]: t["delay_seconds"] for t in live_trip_summaries}
    stats = {
        "trip_updates_seen": len(live_trip_summaries),
        "stops_finalized": len(finalized),
        "eol_stops_excluded": eol_excluded,
    }
    return stats, trip_delay_by_id


def _blank_counts():
    # delay_sum/delay_sample_count let the dashboard compute an average
    # signed delay (seconds, +late/-early). delay_sample_count is tracked
    # separately from "total" rather than reused, because a day's rollup
    # file can already exist on disk from before these two fields existed --
    # "total" on such a file already includes events whose raw delay was
    # never recorded (lost; not recoverable), so dividing by "total" would
    # silently understate the true average. delay_sample_count only counts
    # events that actually contributed to delay_sum, so delay_sum /
    # delay_sample_count stays accurate through that transition, and the two
    # simply converge with total once a rollup file is entirely post-upgrade.
    return {"on_time": 0, "early": 0, "late": 0, "total": 0, "delay_sum": 0, "delay_sample_count": 0}


def _bump(d, bucket, delay):
    d[bucket] += 1
    d["total"] += 1
    # get(..., 0), not += : `d` may be an entry loaded from a rollup file
    # written before these two fields existed, in which case the keys are
    # simply absent rather than zero.
    d["delay_sum"] = d.get("delay_sum", 0) + delay
    d["delay_sample_count"] = d.get("delay_sample_count", 0) + 1


def apply_rollup(finalized, routes, stops):
    """Roll each finalized stop-time event into three views for the day:
    system-wide, per-route, and per-stop (with a per-route breakdown nested
    inside each stop, since a stop can be served by more than one route)."""
    date = service_date()
    path = PERF_DIR / f"{date}.json"
    rollup = load_json(path, {
        "date": date,
        "system": _blank_counts(),
        "routes": {},
        "stops": {},
    })

    for entry in finalized:
        bucket = classify(entry["delay"])
        route_id = entry["route_id"]
        stop_id = entry["stop_id"] or f"seq:{entry['stop_sequence']}"
        route_name = routes.get(route_id, {}).get("short_name", route_id)
        stop_name = stops.get(entry["stop_id"], {}).get("name", stop_id) if entry["stop_id"] else f"Stop sequence {entry['stop_sequence']}"

        _bump(rollup["system"], bucket, entry["delay"])

        r = rollup["routes"].setdefault(route_id, {"route_short_name": route_name, **_blank_counts()})
        _bump(r, bucket, entry["delay"])

        s = rollup["stops"].setdefault(stop_id, {"stop_name": stop_name, "routes": {}, **_blank_counts()})
        _bump(s, bucket, entry["delay"])
        sr = s["routes"].setdefault(route_id, {"route_short_name": route_name, **_blank_counts()})
        _bump(sr, bucket, entry["delay"])

    rollup["last_updated"] = now_iso()
    save_json(path, rollup)


def process_vehicle_positions(feed, routes, trip_delay_by_id):
    vehicles = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        route_id = v.trip.route_id if v.HasField("trip") else ""
        trip_id = v.trip.trip_id if v.HasField("trip") else ""
        delay = trip_delay_by_id.get(trip_id)
        # VehicleDescriptor.id is a random UUID (same scheme as trip_id), not
        # a rider-facing fleet number -- confirmed against the live feed
        # while building the sibling gocary-service-loss-monitor project.
        # VehicleDescriptor.label holds GoCary's actual bus number (e.g.
        # "1531"), so prefer it and only fall back to id if label is empty.
        vehicle_id = entity.id
        if v.HasField("vehicle"):
            vehicle_id = v.vehicle.label if v.vehicle.label else v.vehicle.id
        vehicles.append({
            "vehicle_id": vehicle_id,
            "trip_id": trip_id,
            "route_id": route_id,
            "route_short_name": routes.get(route_id, {}).get("short_name", route_id),
            "route_color": routes.get(route_id, {}).get("color", "2563eb"),
            "lat": v.position.latitude,
            "lon": v.position.longitude,
            "bearing": v.position.bearing if v.position.HasField("bearing") else None,
            "speed": v.position.speed if v.position.HasField("speed") else None,
            "timestamp": v.timestamp if v.HasField("timestamp") else None,
            "delay_seconds": delay,
            "status": classify(delay) if delay is not None else "unknown",
        })
    save_json(LIVE_DIR / "vehicles.json", {"updated_at": now_iso(), "vehicles": vehicles})
    return len(vehicles)


def process_alerts(feed, routes):
    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        a = entity.alert
        header = a.header_text.translation[0].text if a.header_text.translation else ""
        desc = a.description_text.translation[0].text if a.description_text.translation else ""
        affected_routes = []
        for ie in a.informed_entity:
            if ie.route_id:
                affected_routes.append(routes.get(ie.route_id, {}).get("short_name", ie.route_id))
        alerts.append({
            "id": entity.id,
            "header": header,
            "description": desc,
            "effect": gtfs_realtime_pb2.Alert.Effect.Name(a.effect),
            "routes": affected_routes,
        })
    save_json(LIVE_DIR / "alerts.json", {"updated_at": now_iso(), "alerts": alerts})
    return len(alerts)


def write_performance_index():
    """GitHub Pages doesn't serve a directory listing, so the dashboard has
    no static fallback for discovering which performance/*.json files exist
    -- it has to ask the GitHub Contents API, which is capped at 60
    unauthenticated requests/hour per IP and silently returns nothing useful
    once that's exhausted. Write the list ourselves instead, so the
    dashboard can fetch this one JSON file the same resilient way it fetches
    any other (Contents API first, falling back to the Pages-served copy)."""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    dates = sorted(p.stem for p in PERF_DIR.glob("*.json") if date_pattern.match(p.stem))
    save_json(PERF_DIR / "index.json", {"dates": dates})


def main():
    status = load_json(STATUS_FILE, {
        "last_polled": None,
        "last_error": None,
        "polls_run": 0,
    })

    try:
        routes, stops, trip_last_stops = ensure_static_gtfs()
        vp_feed = fetch_feed(VEHICLE_POSITIONS_URL)
        tu_feed = fetch_feed(TRIP_UPDATES_URL)
        alerts_feed = fetch_feed(SERVICE_ALERTS_URL)

        tu_stats, trip_delay_by_id = process_trip_updates(tu_feed, routes, stops, trip_last_stops)
        vehicle_count = process_vehicle_positions(vp_feed, routes, trip_delay_by_id)
        alert_count = process_alerts(alerts_feed, routes)
        write_performance_index()

        status["last_error"] = None
        status["vehicles_seen"] = vehicle_count
        status["alerts_active"] = alert_count
        status.update(tu_stats)
        print(f"Polled OK: {vehicle_count} vehicles, {alert_count} alerts, "
              f"{tu_stats['stops_finalized']} stop(s) finalized into today's rollup.")
    except Exception as exc:
        status["last_error"] = f"{now_iso()}: {exc}"
        status["last_polled"] = now_iso()
        status["polls_run"] += 1
        save_json(STATUS_FILE, status)
        raise

    status["last_polled"] = now_iso()
    status["polls_run"] += 1
    save_json(STATUS_FILE, status)


if __name__ == "__main__":
    main()
