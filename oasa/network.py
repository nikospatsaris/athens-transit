"""Ingest the static network: lines -> routes -> ordered stops.

Run this occasionally (the network changes a few times a year), not on a loop.
"""

import json
import logging
import os
import time

from .client import as_float
from .events import haversine

log = logging.getLogger("oasa.network")

WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "watchlist.json")


def load_watchlist(path=WATCHLIST_PATH):
    with open(path, encoding="utf-8") as handle:
        return [str(x).strip() for x in json.load(handle)["lines"]]


SAME_TERMINAL_M = 600.0


def assign_directions(conn, route_codes):
    """Label the routes of one line as 'go' or 'come' by where they start.

    The API does not say which way a route runs. Taking the first-listed route as
    outbound and everything else as inbound looks fine until a line has variants:
    on line 040 the night route PEIRAIAS - SYNTAGMA [22:00-06:00] is listed third
    and runs the same way as the first route, so position alone mislabels it and
    its departures then get matched against the wrong half of the timetable.

    Origin location is the reliable signal instead -- routes leaving from the same
    terminal run the same way. Which group gets called 'go' still follows the API
    ordering, because nothing in the feed defines it; it is a label, not a fact.
    """
    origins = {}
    for route_code in route_codes:
        row = conn.execute(
            """SELECT s.lat, s.lng FROM route_stops rs JOIN stops s USING(stop_code)
               WHERE rs.route_code=? ORDER BY rs.stop_order LIMIT 1""", (route_code,)
        ).fetchone()
        if row:
            origins[route_code] = (row["lat"], row["lng"])

    reference = next((origins[rc] for rc in route_codes if rc in origins), None)
    for route_code in route_codes:
        origin = origins.get(route_code)
        if origin is None or reference is None:
            direction = "go"
        else:
            distance = haversine(reference[0], reference[1], origin[0], origin[1])
            direction = "go" if distance <= SAME_TERMINAL_M else "come"
        conn.execute("UPDATE routes SET direction=? WHERE route_code=?",
                     (direction, route_code))


def ingest(client, conn, watchlist=None):
    watchlist = watchlist if watchlist is not None else load_watchlist()
    wanted = {x.strip().upper() for x in watchlist}
    now = time.time()

    all_lines = client.lines_with_masterline()
    log.info("API returned %d lines; watchlist wants %d", len(all_lines), len(wanted))

    selected = [ln for ln in all_lines if str(ln.get("line_id", "")).strip().upper() in wanted]
    if not selected:
        raise SystemExit("No watchlist line matched the API. Check watchlist.json.")

    missing = wanted - {str(ln["line_id"]).strip().upper() for ln in selected}
    if missing:
        log.warning("not found in the API, skipping: %s", ", ".join(sorted(missing)))

    for line in selected:
        conn.execute(
            """INSERT INTO lines (line_code, line_id, ml_code, sdc_code, descr, descr_en, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(line_code) DO UPDATE SET
                 line_id=excluded.line_id, ml_code=excluded.ml_code, sdc_code=excluded.sdc_code,
                 descr=excluded.descr, descr_en=excluded.descr_en, fetched_at=excluded.fetched_at""",
            (str(line["line_code"]), str(line["line_id"]).strip(), str(line.get("ml_code") or ""),
             str(line.get("sdc_code") or ""), line.get("line_descr"), line.get("line_descr_eng"), now),
        )
    conn.commit()
    log.info("stored %d line records (a line number can map to several line codes)", len(selected))

    route_count = stop_link_count = 0
    for line in selected:
        line_code = str(line["line_code"])
        routes = client.routes(line_code)
        if not routes:
            log.warning("line_code %s (%s) has no routes", line_code, line["line_id"])
            continue

        ordered_route_codes = []
        for route in routes:
            route_code = str(route["RouteCode"])
            ordered_route_codes.append(route_code)
            conn.execute(
                """INSERT INTO routes (route_code, line_code, direction, descr, descr_en, distance_m, fetched_at)
                   VALUES (?,?,NULL,?,?,?,?)
                   ON CONFLICT(route_code) DO UPDATE SET
                     line_code=excluded.line_code,
                     descr=excluded.descr, descr_en=excluded.descr_en,
                     distance_m=excluded.distance_m, fetched_at=excluded.fetched_at""",
                (route_code, line_code, route.get("RouteDescr"),
                 route.get("RouteDescrEng"), as_float(route.get("RouteDistance")), now),
            )
            route_count += 1

            stops = client.stops(route_code)
            if not stops:
                log.warning("route %s has no stops", route_code)
                continue

            conn.execute("DELETE FROM route_stops WHERE route_code=?", (route_code,))
            for stop in stops:
                stop_code = str(stop["StopCode"])
                lat, lng = as_float(stop.get("StopLat")), as_float(stop.get("StopLng"))
                if lat is None or lng is None:
                    log.warning("stop %s on route %s has no coordinates, skipped", stop_code, route_code)
                    continue
                conn.execute(
                    """INSERT INTO stops (stop_code, stop_id, descr, descr_en, lat, lng, fetched_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(stop_code) DO UPDATE SET
                         descr=excluded.descr, descr_en=excluded.descr_en,
                         lat=excluded.lat, lng=excluded.lng, fetched_at=excluded.fetched_at""",
                    (stop_code, str(stop.get("StopID") or ""), stop.get("StopDescr"),
                     stop.get("StopDescrEng"), lat, lng, now),
                )
                order = int(stop["RouteStopOrder"])
                conn.execute(
                    """INSERT OR REPLACE INTO route_stops (route_code, stop_code, stop_order)
                       VALUES (?,?,?)""",
                    (route_code, stop_code, order),
                )
                stop_link_count += 1

        assign_directions(conn, ordered_route_codes)
        conn.commit()

    log.info("stored %d routes and %d route-stop links", route_count, stop_link_count)
    return {"lines": len(selected), "routes": route_count, "route_stops": stop_link_count}
