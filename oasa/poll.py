"""Poll live vehicle positions for the watched routes.

The API reports each vehicle's own GPS timestamp (CS_DATE), which updates roughly
every 30-60 seconds. Polling faster than that returns the same fix again, so
positions are keyed on (route, vehicle, gps_time) and re-inserts are ignored --
duplicate suppression happens in the database, not in the loop.

Nothing here is destructive: the table is append-only, so stopping and restarting
the poller just leaves a gap in the day.
"""

import logging
import signal
import time

from .client import ApiError, as_float, parse_api_datetime
from .tz import to_epoch

log = logging.getLogger("oasa.poll")

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    log.info("stop requested, finishing this cycle")


def watched_routes(conn):
    return conn.execute(
        "SELECT r.route_code, r.direction, l.line_id FROM routes r "
        "JOIN lines l USING(line_code) ORDER BY l.line_id, r.direction"
    ).fetchall()


def poll_once(client, conn):
    routes = watched_routes(conn)
    started = time.time()
    cursor = conn.execute(
        "INSERT INTO poll_cycles (started_at, routes) VALUES (?,?)", (started, len(routes))
    )
    cycle_id = cursor.lastrowid

    seen = new = errors = 0
    before = conn.total_changes
    for route in routes:
        try:
            vehicles = client.bus_locations(route["route_code"])
        except ApiError as exc:
            log.warning("route %s (line %s): %s", route["route_code"], route["line_id"], exc)
            errors += 1
            continue

        for vehicle in vehicles:
            stamp = parse_api_datetime(vehicle.get("CS_DATE"))
            lat = as_float(vehicle.get("CS_LAT"))
            lng = as_float(vehicle.get("CS_LNG"))
            if stamp is None or lat is None or lng is None:
                log.debug("dropping incomplete fix: %r", vehicle)
                continue
            seen += 1
            conn.execute(
                """INSERT OR IGNORE INTO vehicle_positions
                   (route_code, veh_no, cs_epoch, lat, lng, seen_epoch) VALUES (?,?,?,?,?,?)""",
                (route["route_code"], str(vehicle["VEH_NO"]), to_epoch(stamp), lat, lng, started),
            )

    new = conn.total_changes - before
    conn.execute(
        "UPDATE poll_cycles SET finished_at=?, rows_seen=?, rows_new=?, errors=? WHERE id=?",
        (time.time(), seen, new, errors, cycle_id),
    )
    conn.commit()
    return {"routes": len(routes), "seen": seen, "new": new, "errors": errors,
            "elapsed": time.time() - started}


def run(client, conn, interval=30.0, duration=None, cycles=None):
    global _stop
    _stop = False          # so a second run() in the same process is not a no-op
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass  # not available on every platform

    deadline = time.time() + duration if duration else None
    totals = {"cycles": 0, "seen": 0, "new": 0, "errors": 0}

    while not _stop:
        stats = poll_once(client, conn)
        totals["cycles"] += 1
        for key in ("seen", "new", "errors"):
            totals[key] += stats[key]
        log.info("cycle %d: %d routes, %d fixes, %d new, %d errors, %.1fs",
                 totals["cycles"], stats["routes"], stats["seen"], stats["new"],
                 stats["errors"], stats["elapsed"])

        if cycles and totals["cycles"] >= cycles:
            break
        if deadline and time.time() >= deadline:
            break

        sleep_for = max(0.0, interval - stats["elapsed"])
        if deadline:
            sleep_for = min(sleep_for, max(0.0, deadline - time.time()))
        # wake up promptly on Ctrl-C instead of sleeping through it
        end = time.time() + sleep_for
        while time.time() < end and not _stop:
            time.sleep(min(0.5, end - time.time()))

    return totals
