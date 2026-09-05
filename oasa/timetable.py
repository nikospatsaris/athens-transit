"""Ingest the planned timetable for the watched lines.

`getSchedLines` answers for one service-day pattern at a time -- the sdc_code that
`webGetLinesWithMLInfo` reports for the line *today*. So this is a snapshot of
today's plan, not a full weekly timetable, and it must be re-run when the service
day changes (weekday / Saturday / Sunday).

A record in the response is a *vehicle duty*, not a trip: `sde_start1`/`sde_end1`
is the outbound leg and `sde_start2`/`sde_end2` the return leg of the same bus.
The API lists each duty under both keys, so the same sde_code shows up in `go` and
in `come` -- reading leg 1 in both places (or keying on sde_code alone) silently
throws half the timetable away. The outbound departure is start1, the return
departure is start2.

Departure times come back stamped on a placeholder date ('1900-01-01 05:15:00'),
so only the time of day is meaningful. They are stored as minutes after midnight.
"""

import logging
import time
from datetime import datetime

log = logging.getLogger("oasa.timetable")


def _minutes(raw):
    """'1900-01-01 05:15:00' -> 315. Returns None for the blank rows the API emits."""
    if not raw:
        return None
    try:
        stamp = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        log.warning("unparseable schedule time: %r", raw)
        return None
    return stamp.hour * 60 + stamp.minute


def ingest(client, conn):
    now = time.time()
    lines = conn.execute(
        "SELECT line_code, line_id, ml_code, sdc_code FROM lines "
        "WHERE ml_code <> '' AND sdc_code <> ''"
    ).fetchall()

    stored = skipped = 0
    for line in lines:
        payload = client.schedule(line["ml_code"], line["sdc_code"], line["line_code"])
        for direction, leg in (("go", "1"), ("come", "2")):
            for entry in payload[direction]:
                start = _minutes(entry.get("sde_start" + leg))
                if start is None:
                    # A duty listed under one key but with no leg on that side:
                    # the bus deadheads in and only runs the other direction.
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO scheduled_trips
                       (sde_code, line_code, direction, sdc_code, start_min, end_min, note, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (str(entry.get("sde_code")), line["line_code"], direction,
                     line["sdc_code"], start, _minutes(entry.get("sde_end1")),
                     entry.get("sde_descr1"), now),
                )
                stored += 1
        conn.commit()

    log.info("stored %d scheduled trips (%d rows had no departure time)", stored, skipped)
    return {"scheduled_trips": stored, "skipped": skipped}


def headways(conn, line_code, direction):
    """Planned gap between consecutive departures, in minutes."""
    rows = conn.execute(
        "SELECT start_min FROM scheduled_trips WHERE line_code=? AND direction=? "
        "ORDER BY start_min", (line_code, direction)
    ).fetchall()
    times = [r["start_min"] for r in rows]
    return [b - a for a, b in zip(times, times[1:]) if 0 < b - a <= 180]
