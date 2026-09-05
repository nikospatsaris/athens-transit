"""Turn observed events into service-quality numbers.

Two questions, deliberately kept apart:

1. Did buses leave when the timetable said?  Answered by matching observed
   terminal departures to scheduled ones. Honest only for the trips whose
   departure was actually witnessed.

2. Did buses arrive evenly?  On a line running every 9 minutes nobody reads the
   timetable, so punctuality is the wrong question -- what a passenger feels is
   the gap between buses. That is measured from arrivals alone and needs no
   schedule at all, which makes it the more robust of the two.

The headline number for (2) is the excess wait a passenger pays for irregularity.
For arrivals at a random moment the mean wait is E[h^2] / (2 E[h]), not E[h]/2:
the two agree only when every gap is identical, and clumping pushes the first
above the second even though the same buses run. The penalty reported here is
that difference, taken against the observed mean headway -- see excess_wait() for
why the timetable is the wrong baseline for it.
"""

import logging
import statistics
from collections import defaultdict

from .timetable import headways as scheduled_headways
from .tz import from_epoch

log = logging.getLogger("oasa.analyze")

MAX_HEADWAY_MIN = 120.0   # a longer gap is a service break, not a headway
MIN_HEADWAYS = 3          # below this, spread statistics are noise
BUNCHING_RATIO = 0.25     # arrivals closer than a quarter of the planned gap
ON_TIME_EARLY = -1.0      # transit convention: 1 min early to 5 late is on time
ON_TIME_LATE = 5.0
MATCH_WINDOW_MIN = 30.0   # further than this from any scheduled trip: unmatched


def observation_window(conn):
    row = conn.execute(
        "SELECT MIN(cs_epoch) lo, MAX(cs_epoch) hi FROM vehicle_positions").fetchone()
    return (row["lo"], row["hi"]) if row and row["lo"] else (None, None)


def _minute_of_day(epoch):
    local = from_epoch(epoch)
    return local.hour * 60 + local.minute + local.second / 60.0


def observed_headways(conn, route_code):
    """Gaps between consecutive arrivals, per stop, in minutes.

    Keyed on position in the route, not just the stop code: a route that starts
    and ends at the same stop lists it twice, and differencing an arrival at
    stop-order 1 against one at stop-order 60 measures nothing at all.

    Each gap is flagged with whether the two arrivals were different vehicles.
    They are not always: on a circular line worked by one bus, every consecutive
    pair at a stop is that same bus coming round again, which is a round-trip
    cycle time and not a headway between services.
    """
    rows = conn.execute(
        "SELECT stop_code, stop_order, veh_no, event_epoch FROM stop_events "
        "WHERE route_code=? ORDER BY stop_code, stop_order, event_epoch", (route_code,)
    ).fetchall()

    by_stop = defaultdict(list)
    for row in rows:
        by_stop[(row["stop_code"], row["stop_order"])].append((row["event_epoch"], row["veh_no"]))

    gaps = []
    for (stop_code, _order), visits in by_stop.items():
        for (earlier, first_veh), (later, second_veh) in zip(visits, visits[1:]):
            minutes = (later - earlier) / 60.0
            # A zero gap means two buses at the same stop in the same sample:
            # real bunching, keep it. A huge gap is the end of service, drop it.
            if 0 <= minutes <= MAX_HEADWAY_MIN:
                gaps.append((stop_code, minutes, first_veh != second_veh))
    return gaps


def _scheduled_headway_in_window(conn, line_code, direction, window):
    """Planned gap during the hours actually observed, not the daily average."""
    lo, hi = window
    if lo is None:
        return None
    start, end = _minute_of_day(lo), _minute_of_day(hi)
    rows = conn.execute(
        "SELECT start_min FROM scheduled_trips WHERE line_code=? AND direction=? "
        "ORDER BY start_min", (line_code, direction)
    ).fetchall()
    times = [r["start_min"] for r in rows]
    if end < start:                       # window crossed midnight
        times = [t for t in times if t >= start - 60 or t <= end]
    else:
        times = [t for t in times if start - 60 <= t <= end + 60]
    gaps = [b - a for a, b in zip(times, times[1:]) if 0 < b - a <= MAX_HEADWAY_MIN]
    if not gaps:
        gaps = scheduled_headways(conn, line_code, direction)
    return statistics.median(gaps) if gaps else None


def excess_wait(gaps):
    """Actual mean wait, the wait an even service would give, and the penalty.

    The penalty is measured against the frequency *observed*, not the frequency
    planned. Measuring against the timetable conflates two different failures:
    a line running fewer buses than promised and a line running them in clumps.
    It also produces negative numbers whenever more buses happen to pass than the
    timetable calls for, which reads as though bunching were somehow beneficial.

    Compared against its own mean headway the penalty is >= 0 by Cauchy-Schwarz,
    and is zero exactly when every gap is identical. Frequency is still visible:
    the scheduled and observed headways sit next to it in the report.
    """
    total = sum(gaps)
    if total <= 0:
        return None, None, None
    actual = sum(g * g for g in gaps) / (2 * total)
    even = statistics.mean(gaps) / 2.0
    return actual, even, actual - even


def route_summary(conn):
    window = observation_window(conn)
    rows = conn.execute(
        """SELECT r.route_code, r.line_code, r.direction, r.descr_en, l.line_id
           FROM routes r JOIN lines l USING(line_code) ORDER BY l.line_id, r.direction"""
    ).fetchall()

    out = []
    for route in rows:
        events = conn.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT veh_no) v FROM stop_events WHERE route_code=?",
            (route["route_code"],)
        ).fetchone()
        if not events["c"]:
            continue

        measured = observed_headways(conn, route["route_code"])
        # Only gaps between two different vehicles are headways. Keeping a bus's
        # own lap time in here makes a one-bus circular line look like flawless
        # service: line 021 scored a CV of 0.04 on 16 gaps, every one of them the
        # same vehicle coming round again.
        gaps = [g for _, g, distinct in measured if distinct]
        same_vehicle = sum(1 for _, _, distinct in measured if not distinct)
        planned = _scheduled_headway_in_window(conn, route["line_code"], route["direction"], window)
        entry = {
            "route_code": route["route_code"],
            "line_id": route["line_id"],
            "direction": route["direction"],
            "descr": route["descr_en"],
            "vehicles": events["v"],
            "events": events["c"],
            "headways": len(gaps),
            "same_vehicle_gaps": same_vehicle,
            "scheduled_headway": planned,
            "observed_headway": statistics.median(gaps) if gaps else None,
            "cv": None, "bunched_pct": None,
            "wait": None, "even_wait": None, "penalty": None,
        }
        if len(gaps) >= MIN_HEADWAYS:
            mean = statistics.mean(gaps)
            entry["cv"] = statistics.pstdev(gaps) / mean if mean > 0 else None
            entry["wait"], entry["even_wait"], entry["penalty"] = excess_wait(gaps)
            if planned:
                bunched = sum(1 for g in gaps if g < BUNCHING_RATIO * planned)
                entry["bunched_pct"] = 100.0 * bunched / len(gaps)
        out.append(entry)
    return out


def adherence(conn):
    """Match witnessed terminal departures to the timetable."""
    rows = conn.execute(
        """SELECT d.depart_epoch, r.line_code, r.direction, l.line_id
           FROM trip_departures d JOIN routes r USING(route_code)
           JOIN lines l USING(line_code)"""
    ).fetchall()

    schedules = {}
    results = defaultdict(list)
    unmatched = defaultdict(int)

    for row in rows:
        key = (row["line_code"], row["direction"])
        if key not in schedules:
            schedules[key] = [r["start_min"] for r in conn.execute(
                "SELECT start_min FROM scheduled_trips WHERE line_code=? AND direction=?",
                key).fetchall()]
        planned = schedules[key]
        if not planned:
            unmatched[row["line_id"]] += 1
            continue

        actual = _minute_of_day(row["depart_epoch"])
        # Compare across midnight as well: a 00:05 departure is 10 minutes late
        # off a 23:55 slot, not 1435 minutes early.
        best = min(planned, key=lambda p: min(abs(actual - p), 1440 - abs(actual - p)))
        delay = actual - best
        if delay > 720:
            delay -= 1440
        elif delay < -720:
            delay += 1440

        if abs(delay) > MATCH_WINDOW_MIN:
            unmatched[row["line_id"]] += 1
            continue
        results[(row["line_id"], row["direction"])].append(delay)

    summary = []
    for (line_id, direction), delays in sorted(results.items()):
        on_time = sum(1 for d in delays if ON_TIME_EARLY <= d <= ON_TIME_LATE)
        summary.append({
            "line_id": line_id,
            "direction": direction,
            "departures": len(delays),
            "median_delay": statistics.median(delays),
            "on_time_pct": 100.0 * on_time / len(delays),
            "worst": max(delays),
        })
    return summary, dict(unmatched)


def coverage(conn):
    """What the numbers rest on, so a thin sample is visible rather than implied."""
    window = observation_window(conn)
    cycles = conn.execute(
        "SELECT COUNT(*) n, SUM(rows_new) new, SUM(rows_seen) seen, SUM(errors) err "
        "FROM poll_cycles WHERE finished_at IS NOT NULL").fetchone()
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM vehicle_positions) positions,"
        " (SELECT COUNT(DISTINCT veh_no) FROM vehicle_positions) vehicles,"
        " (SELECT COUNT(*) FROM stop_events) events,"
        " (SELECT SUM(exact) FROM stop_events) exact,"
        " (SELECT COUNT(*) FROM trip_departures) departures,"
        " (SELECT COUNT(*) FROM scheduled_trips) scheduled").fetchone()
    return {
        "from": from_epoch(window[0]) if window[0] else None,
        "to": from_epoch(window[1]) if window[1] else None,
        "hours": (window[1] - window[0]) / 3600.0 if window[0] else 0.0,
        "cycles": cycles["n"] or 0,
        "fetched": cycles["seen"] or 0,
        "stored": cycles["new"] or 0,
        "errors": cycles["err"] or 0,
        **dict(counts),
    }
