"""Derive stop arrivals from sampled GPS fixes.

The feed gives positions, not events: a bus reports a fix every 30-60s and at
30 km/h that is up to 500 m of unobserved travel, so a bus can pass a stop
entirely between two fixes. Rather than asking "was the bus within X metres of a
stop", this walks the stop sequence of the route and asks which stops the bus has
got past, then interpolates the crossing time along the chain.

Consequences worth knowing before trusting the numbers:
  * an arrival is 'exact' only when a fix landed near the stop; otherwise the
    timestamp is interpolated and carries the sampling error
  * a trip already in progress when polling started has no observable departure
  * vehicles are matched to the route the API assigned them to, so a bus
    reassigned mid-trip looks like two short trips
"""

import logging
import math
from collections import defaultdict

log = logging.getLogger("oasa.events")

EARTH_RADIUS_M = 6371008.8

MAX_SNAP_M = 1200.0     # further than this from every stop: off-route, ignore
NEAR_STOP_M = 250.0     # close enough to call the arrival observed, not inferred
LOOKAHEAD = 15          # stops a bus may plausibly pass between two fixes
BACKTRACK = 2           # tolerated backwards drift from GPS jitter
NEW_TRIP_DROP = 5       # stops backwards before we call it a new trip
MAX_GAP_S = 20 * 60     # longer silence than this breaks the trip
TERMINAL_INDEX = 1      # a trip must start at or before this stop to be a departure
DEPARTED_INDEX = 3      # ...and must reach this one, so parked buses do not count


def haversine(lat1, lng1, lat2, lng2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def route_geometry(conn, route_code):
    """Ordered stops plus cumulative distance along the stop chain."""
    rows = conn.execute(
        """SELECT rs.stop_order, rs.stop_code, s.lat, s.lng
           FROM route_stops rs JOIN stops s USING(stop_code)
           WHERE rs.route_code=? ORDER BY rs.stop_order""", (route_code,)
    ).fetchall()
    stops = [dict(r) for r in rows]
    cumulative = 0.0
    for index, stop in enumerate(stops):
        if index:
            previous = stops[index - 1]
            cumulative += haversine(previous["lat"], previous["lng"], stop["lat"], stop["lng"])
        stop["cum_m"] = cumulative
    return stops


def _nearest(stops, lat, lng, lo, hi):
    best_index, best_distance = None, float("inf")
    for index in range(max(0, lo), min(len(stops), hi)):
        stop = stops[index]
        distance = haversine(lat, lng, stop["lat"], stop["lng"])
        if distance < best_distance:
            best_index, best_distance = index, distance
    return best_index, best_distance


def _events_for_vehicle(stops, positions):
    """Yield (stop_index, epoch, exact, trip_seq) for one vehicle on one route."""
    trip_seq = 0
    previous_index = None
    previous_time = None

    for epoch, lat, lng in positions:
        if previous_index is None:
            index, distance = _nearest(stops, lat, lng, 0, len(stops))
            if index is None or distance > MAX_SNAP_M:
                continue
            previous_index, previous_time = index, epoch
            if distance <= NEAR_STOP_M:
                yield index, epoch, 1, trip_seq
            continue

        if epoch - previous_time > MAX_GAP_S:
            trip_seq += 1
            previous_index = None
            previous_time = epoch
            continue

        # Prefer a match just ahead of the last known position, so a bus is not
        # dragged backwards by a stop that happens to be near its path. But a
        # window match can be a mediocre fit hundreds of metres away while the
        # bus is in fact sitting at a stop well behind, having started its return
        # run -- so take the whole-route match when it is decisively better.
        index, distance = _nearest(stops, lat, lng,
                                   previous_index - BACKTRACK, previous_index + LOOKAHEAD)
        far_index, far_distance = _nearest(stops, lat, lng, 0, len(stops))
        # Only reach backwards when the forward match is genuinely poor. Athens
        # routes double back on themselves -- 4361 starts and ends at the same
        # stop -- so a bus in normal service is often near a stop it passed
        # earlier. Without this guard it flip-flops between the two and every
        # flip is counted as a new trip.
        if index is None or distance > MAX_SNAP_M or (
                distance > NEAR_STOP_M and far_distance <= NEAR_STOP_M
                and 2 * far_distance < distance):
            index, distance = far_index, far_distance
        if index is None or distance > MAX_SNAP_M:
            continue

        if previous_index - index >= NEW_TRIP_DROP:
            trip_seq += 1
            previous_index, previous_time = index, epoch
            if distance <= NEAR_STOP_M:
                yield index, epoch, 1, trip_seq
            continue

        if index > previous_index:
            span = stops[index]["cum_m"] - stops[previous_index]["cum_m"]
            for passed in range(previous_index + 1, index + 1):
                if span > 0:
                    fraction = (stops[passed]["cum_m"] - stops[previous_index]["cum_m"]) / span
                else:
                    fraction = 1.0
                crossing = previous_time + fraction * (epoch - previous_time)
                exact = 1 if (passed == index and distance <= NEAR_STOP_M) else 0
                yield passed, crossing, exact, trip_seq
            previous_index, previous_time = index, epoch
        else:
            previous_time = epoch


def rebuild(conn):
    """Recompute every derived event from the stored positions. Idempotent."""
    conn.execute("DELETE FROM stop_events")
    conn.execute("DELETE FROM trip_departures")

    route_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT route_code FROM vehicle_positions").fetchall()]

    total_events = total_departures = 0
    for route_code in route_codes:
        stops = route_geometry(conn, route_code)
        if len(stops) < 2:
            log.warning("route %s has no usable stop geometry, skipped", route_code)
            continue

        by_vehicle = defaultdict(list)
        for row in conn.execute(
            "SELECT veh_no, cs_epoch, lat, lng FROM vehicle_positions "
            "WHERE route_code=? ORDER BY veh_no, cs_epoch", (route_code,)
        ):
            by_vehicle[row["veh_no"]].append((row["cs_epoch"], row["lat"], row["lng"]))

        for veh_no, positions in by_vehicle.items():
            trips = defaultdict(list)
            for index, epoch, exact, trip_seq in _events_for_vehicle(stops, positions):
                stop = stops[index]
                conn.execute(
                    """INSERT OR REPLACE INTO stop_events
                       (route_code, stop_code, veh_no, trip_seq, stop_order, event_epoch, exact)
                       VALUES (?,?,?,?,?,?,?)""",
                    (route_code, stop["stop_code"], veh_no, trip_seq,
                     stop["stop_order"], epoch, exact),
                )
                total_events += 1
                trips[trip_seq].append((index, epoch))

            for trip_seq, seen in trips.items():
                indexes = [i for i, _ in seen]
                # A departure needs both halves of the story: the bus was at the
                # terminal, and it then actually went somewhere. Without the
                # second test every bus parked at a terminus when the poller
                # started would be counted as departing at that moment.
                if min(indexes) > TERMINAL_INDEX or max(indexes) < DEPARTED_INDEX:
                    continue
                moving = [e for i, e in seen if i >= 1]
                if not moving:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO trip_departures
                       (route_code, veh_no, trip_seq, depart_epoch) VALUES (?,?,?,?)""",
                    (route_code, veh_no, trip_seq, min(moving)),
                )
                total_departures += 1
        conn.commit()

    log.info("derived %d stop events and %d observable departures across %d routes",
             total_events, total_departures, len(route_codes))
    return {"stop_events": total_events, "trip_departures": total_departures}
