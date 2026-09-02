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
STATIC_GTFS_URL = "http://data.trilliumtransit.com/gtfs/cary-transit-nc-us/cary-transit-nc-us.zip"

SERVICE_TZ = ZoneInfo("America/New_York")

# A stop-time prediction counts as "on time" if its delay (seconds, GTFS-RT
# StopTimeEvent.delay: positive = late, negative = early) falls in this
# window. Outside it, it's bucketed as early or late. This 1-min-early /
# 5-min-late window is a common transit-industry on-time definition (used by,
# e.g., many TCRP-derived agency standards); adjust here if GoCary uses a
# different one.
EARLY_THRESHOLD_S = -60
LATE_THRESHOLD_S = 300

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LIVE_DIR = DATA_DIR / "live"
PERF_DIR = DATA_DIR / "performance"
ROUTES_FILE = DATA_DIR / "routes.json"
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


def ensure_routes():
    """Refresh docs/data/routes.json from GoCary's static GTFS if it's
    missing or older than a week. Route/stop metadata barely ever changes,
    so this doesn't need to run every poll -- and if the refresh fails we
    just keep whatever we already had rather than failing the whole run."""
    existing = load_json(ROUTES_FILE, None)
    if existing is not None:
        fetched_at = existing.get("_fetched_at")
        if fetched_at:
            age = time.time() - datetime.fromisoformat(fetched_at).timestamp()
            if age < ROUTES_MAX_AGE_S:
                return existing

    try:
        resp = requests.get(STATIC_GTFS_URL, timeout=60)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        routes = {"_fetched_at": now_iso()}
        with zf.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                routes[row["route_id"]] = {
                    "short_name": row.get("route_short_name") or row.get("route_long_name") or row["route_id"],
                    "long_name": row.get("route_long_name") or "",
                    "color": row.get("route_color") or "2563eb",
                }
        save_json(ROUTES_FILE, routes)
        return routes
    except Exception as exc:
        print(f"Warning: failed to refresh routes.json ({exc}); keeping existing copy")
        return existing or {"_fetched_at": None}


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


def process_trip_updates(feed, routes):
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
        apply_rollup(finalized, routes)

    trip_delay_by_id = {t["trip_id"]: t["delay_seconds"] for t in live_trip_summaries}
    stats = {"trip_updates_seen": len(live_trip_summaries), "stops_finalized": len(finalized)}
    return stats, trip_delay_by_id


def apply_rollup(finalized, routes):
    date = service_date()
    path = PERF_DIR / f"{date}.json"
    rollup = load_json(path, {
        "date": date,
        "system": {"on_time": 0, "early": 0, "late": 0, "total": 0},
        "routes": {},
    })

    for entry in finalized:
        bucket = classify(entry["delay"])
        rollup["system"][bucket] += 1
        rollup["system"]["total"] += 1

        route_id = entry["route_id"]
        r = rollup["routes"].setdefault(route_id, {
            "route_short_name": routes.get(route_id, {}).get("short_name", route_id),
            "on_time": 0, "early": 0, "late": 0, "total": 0,
        })
        r[bucket] += 1
        r["total"] += 1

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
        vehicles.append({
            "vehicle_id": v.vehicle.id if v.HasField("vehicle") else entity.id,
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


def main():
    status = load_json(STATUS_FILE, {
        "last_polled": None,
        "last_error": None,
        "polls_run": 0,
    })

    try:
        routes = ensure_routes()
        vp_feed = fetch_feed(VEHICLE_POSITIONS_URL)
        tu_feed = fetch_feed(TRIP_UPDATES_URL)
        alerts_feed = fetch_feed(SERVICE_ALERTS_URL)

        tu_stats, trip_delay_by_id = process_trip_updates(tu_feed, routes)
        vehicle_count = process_vehicle_positions(vp_feed, routes, trip_delay_by_id)
        alert_count = process_alerts(alerts_feed, routes)

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
