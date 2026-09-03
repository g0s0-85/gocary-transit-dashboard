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
# GoCary hosts this static feed themselves, dated 2026-01-23 as of this
# writing -- switched from the Trillium-mirrored copy
# (data.trilliumtransit.com/gtfs/cary-transit-nc-us/), which was dated
# 2023-12-18 and consequently missing routes "2"/"9" and at least 12 stops
# that already had real RT activity (e.g. stop_code 11700, 101, 1913).
# Confirmed this feed's route_id already equals the short display code
# (e.g. route_id "2" -> route_short_name "2"), matching what GoCary's RT
# feed uses directly, and stop_id/stop_code are identical throughout --
# fixes both the stale-route and missing-stop problems in one swap, no
# other code changes needed since the column names line up with what's
# already parsed below.
STATIC_GTFS_URL = "https://www.gocarylive.org/GTFS/google_transit.zip"

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
STATUS_FILE = DATA_DIR / "status.json"
PENDING_FILE = LIVE_DIR / "pending_stops.json"

ROUTES_MAX_AGE_S = 7 * 24 * 3600


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def service_date():
    return datetime.now(SERVICE_TZ).date().isoformat()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_feed(url):
    resp = requests.get(url, timeout=20, headers={"User-Agent": "gocary-transit-dashboard/1.0 (+github actions)"})
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def ensure_static_gtfs():
    """Refresh docs/data/routes.json and docs/data/stops.json from GoCary's
    static GTFS if routes.json is missing or older than a week. Route/stop
    metadata barely ever changes, so this doesn't need to run every poll --
    and if the refresh fails we just keep whatever we already had rather
    than failing the whole run."""
    existing_routes = load_json(ROUTES_FILE, None)
    existing_stops = load_json(STOPS_FILE, None)
    if existing_routes is not None:
        fetched_at = existing_routes.get("_fetched_at")
        if fetched_at:
            age = time.time() - datetime.fromisoformat(fetched_at).timestamp()
            if age < ROUTES_MAX_AGE_S:
                return existing_routes, (existing_stops or {})

    try:
        resp = requests.get(STATIC_GTFS_URL, timeout=60)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))

        fetched_at = now_iso()
        routes = {"_fetched_at": fetched_at}
        with zf.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                routes[row["route_id"]] = {
                    "short_name": row.get("route_short_name") or row.get("route_long_name") or row["route_id"],
                    "long_name": row.get("route_long_name") or "",
                    "color": row.get("route_color") or "2563eb",
                }

        stops = {"_fetched_at": fetched_at}
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

        save_json(ROUTES_FILE, routes)
        save_json(STOPS_FILE, stops)
        return routes, stops
    except Exception as exc:
        print(f"Warning: failed to refresh routes.json/stops.json ({exc}); keeping existing copy")
        return existing_routes or {"_fetched_at": None}, existing_stops or {"_fetched_at": None}


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


def process_trip_updates(feed, routes, stops):
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

    finalized = [v for k, v in pending.items() if k not in current_keys]
    pending = {k: v for k, v in pending.items() if k in current_keys}
    save_json(PENDING_FILE, pending)
    save_json(LIVE_DIR / "trip_updates.json", {
        "updated_at": now_iso(),
        "trips": live_trip_summaries,
    })

    if finalized:
        apply_rollup(finalized, routes, stops)

    trip_delay_by_id = {t["trip_id"]: t["delay_seconds"] for t in live_trip_summaries}
    stats = {"trip_updates_seen": len(live_trip_summaries), "stops_finalized": len(finalized)}
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
        routes, stops = ensure_static_gtfs()
        vp_feed = fetch_feed(VEHICLE_POSITIONS_URL)
        tu_feed = fetch_feed(TRIP_UPDATES_URL)
        alerts_feed = fetch_feed(SERVICE_ALERTS_URL)

        tu_stats, trip_delay_by_id = process_trip_updates(tu_feed, routes, stops)
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
