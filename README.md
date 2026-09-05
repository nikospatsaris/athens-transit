# Athens transit observatory

Measures how evenly Athens buses actually arrive, by recording live vehicle
positions from the OASA telematics API and comparing what happened against the
published timetable.

No API key, no dependencies outside the Python standard library, one SQLite file.

```bash
python run.py network              # lines, routes and stops for the watchlist
python run.py timetable            # today's scheduled departures
python run.py poll --minutes 120   # record live positions
python run.py analyze --html report.html
```

## The question it answers

The obvious thing to measure is punctuality: did the bus leave when the
timetable said? On a line running every nine minutes that is close to
meaningless — nobody consults a timetable for a bus that frequent, and a service
can be perfectly punctual on paper while passengers still wait twenty minutes
and then watch three buses arrive together.

What a passenger actually experiences is the *gap between buses*. So the headline
metric here is *excess wait time*. For passengers turning up at random, the mean
wait is

```
E[h²] / (2·E[h])        not        E[h] / 2
```

where `h` is the headway. The two are equal only when every gap is identical;
any unevenness pushes the first above the second. Excess wait is the difference:
how much longer the average passenger waits than they would if the same number of
buses were spaced evenly. It needs no timetable at all, which makes it robust to
the parts of the feed that are least trustworthy.

The baseline is the *observed* mean headway, not the timetable's. Measuring
against the timetable mixes two unrelated failures — running fewer buses than
promised, and running them in clumps — and goes negative whenever more buses pass
than the timetable calls for, which reads as though bunching had helped. Against
its own mean the penalty is zero for perfectly even service and never negative,
and frequency stays visible in the scheduled and observed headway columns beside
it.

Punctuality is still reported, but only for trips whose departure was actually
witnessed — see *What this does not know*.

## The data source

`https://telematics.oasa.gr/api/` — the backend of the official OASA arrivals
site. It needs no key and no registration.

| what | endpoint |
|---|---|
| lines, joined to master line and service-day code | `webGetLinesWithMLInfo` |
| directions of a line | `webGetRoutes` |
| ordered stops of a route, with coordinates | `webGetStops` |
| live vehicle positions | `getBusLocation` |
| planned departures | `getSchedLines` |

Because it is unauthenticated, the only thing stopping a client from flooding it
is the client. This one sends a single request at a time with a minimum gap
between them, retries only transport errors and 5xx with exponential backoff, and
identifies itself in the `User-Agent`. **Put a real contact address in
`--user-agent` before running it for any length of time.** The watchlist exists
for the same reason: every line adds two calls per poll cycle, so
`watchlist.json` deliberately holds a handful of lines rather than all 469.

## How it works

```
webGetLinesWithMLInfo ─┐
webGetRoutes           ├─→  network    →  lines, routes, stops, route_stops
webGetStops            ─┘
getSchedLines          ──→  timetable  →  scheduled_trips
getBusLocation         ──→  poll       →  vehicle_positions   (append-only)
                                │
                                ▼
                            events     →  stop_events, trip_departures
                                │
                                ▼
                            analyze    →  headways, excess wait, adherence
```

The stages are separate commands on purpose. `poll` is the only one that has to
run continuously, and it does nothing but append raw fixes — every
interpretation happens later in `events`, which is a pure function of the stored
positions and can be re-run after any change to the derivation logic without
re-collecting a day of data.

## Turning positions into arrivals

The feed reports positions, not events. A bus sends a fix every 30–60 seconds,
which at 30 km/h is up to 500 m of unobserved travel — so asking "was a bus ever
within X metres of this stop" misses stops that were passed between two fixes,
and the error is worst exactly when buses are moving fastest.

Instead, `events.py` walks the stop sequence of the route: it tracks which stop
each vehicle is nearest, and when that index advances it records arrivals at
every stop in between, interpolating the crossing times along the cumulative
distance of the stop chain. Arrivals are flagged `exact` when a fix genuinely
landed near the stop, and the report always shows how many of each it used.

Departures are stricter. A departure is only recorded when a bus was seen at a
terminal *and then seen leaving it*. Without that second condition every bus
parked at a terminus when the poller started would be counted as departing at
that moment — in a ten-minute test window that single missing check produced
nine departures, all of them fictional.

## The parts that fought back

Most of the work in this project was not the statistics. It was this:

**The timetable is duties, not trips.** `getSchedLines` returns records with
`sde_start1`/`sde_end1` *and* `sde_start2`/`sde_end2`, and lists the same record
under both `go` and `come`. A record is one bus's rotation: out on leg 1, back on
leg 2. Reading leg 1 in both places — and keying rows on `sde_code`, which is not
unique across directions — silently threw away half the timetable and produced
lines with 20 departures one way and 84 the other. The giveaway was the
asymmetry, not an error message.

**Direction is not in the feed.** The API never says which way a route runs.
Taking the first-listed route as outbound looks fine until a line has variants:
on line 040 the night route `PEIRAIAS - SYNTAGMA [22:00-06:00]` is listed *third*
and runs the same way as the first, so a positional rule labels it inbound and
matches its departures against the wrong half of the timetable. Routes are
grouped by where they start instead — same origin terminal, same direction.
Which group is called `go` still follows API ordering, because nothing in the
feed defines it. It is a label, not a fact.

**Routes double back on themselves.** Route 4361 begins and ends at the same
stop, and stop `11730` therefore appears twice in its sequence. Two consequences,
both of which produced plausible-looking nonsense rather than an error. Grouping
arrivals by stop code alone differenced an arrival at stop-order 1 against one at
stop-order 60 and called the result a headway; headways are keyed on position in
the route instead. And a bus finishing its run sits next to the *first* stop
while legitimately at the last, so matching it to the nearest stop anywhere on
the route dragged it backwards, split one trip into several, and re-emitted a
whole second set of arrivals on the way forward again — four vehicles produced
nine trips in eleven minutes, and line 732 showed 123 headways where every other
route had one or two. Matching now only reaches backwards when the forward match
is genuinely poor.

**A headway needs two buses.** Line 021 is circular and was worked by a single
vehicle, so every consecutive pair of arrivals at a stop was that same bus coming
round again. Differenced blindly, its 16 lap times gave a coefficient of
variation of 0.04 and a waiting penalty of zero — the line scored as the most
perfectly regular service observed, on the strength of having almost no service
at all. Gaps are now flagged by whether the two arrivals were different vehicles,
lap times are excluded from the regularity statistics, and a route left with
nothing to measure reports a dash instead of a flattering number.

**Repeat fixes.** Polling every 30 s when vehicles report every 30–60 s means
most responses repeat positions already seen. Positions are keyed on
`(route, vehicle, gps_timestamp)` and re-inserts are ignored, so deduplication
happens in the schema rather than in the polling loop. Typically a quarter to a
half of everything fetched is already known.

**Timestamps.** `CS_DATE` arrives as `Sep  4 2026 10:13:21:000PM` — padded day,
milliseconds joined by a colon, 12-hour clock, no timezone. It is Athens wall
time. Windows ships no IANA timezone database and `tzdata` is not installed by
default, so `zoneinfo.ZoneInfo("Europe/Athens")` fails on a stock Python; rather
than add a dependency, `tz.py` implements the EU rule directly (EET, EEST from
the last Sunday of March to the last Sunday of October) and is tested against
both changeovers.

**Empty means empty.** A perfectly valid response is the bare JSON literal
`null` — for a line with no routes, a route with no buses running, or a stale
code. That is data, not an error, and the client normalises it to an empty list.

## Data model

| table | what it holds |
|---|---|
| `lines`, `routes`, `stops`, `route_stops` | the network; refreshed occasionally |
| `scheduled_trips` | planned departures, minutes after midnight |
| `vehicle_positions` | raw GPS fixes, append-only, deduplicated by key |
| `poll_cycles` | one row per cycle: fetched, stored, errors |
| `stop_events`, `trip_departures` | derived; safe to delete and rebuild |

WAL mode is on, so `analyze` can read while `poll` is still writing.

## What this does not know

- **Arrival times are inferred.** Any arrival not flagged `exact` was
  interpolated between two fixes and carries the sampling error.
- **Departures are only those witnessed.** A trip already under way when polling
  started has no observable departure, so adherence covers a biased subset —
  trips that began inside the observation window.
- **The timetable is today's pattern only.** `getSchedLines` answers for the
  service-day code currently in effect. Re-run `timetable` when the service day
  changes (weekday / Saturday / Sunday).
- **Vehicle reassignment looks like two trips.** Buses are matched to whichever
  route the API assigned them to; a mid-trip reassignment splits the trip.
- **Stop matching is a heuristic.** Vehicles are placed by nearest stop within a
  forward window. On routes that run close to their own path this can still
  misplace a fix, and the guard against it is a threshold, not a proof. A map
  matcher against the road network would be the real fix.
- **Scope.** A short window over a handful of lines describes those hours on
  those lines. It is not a verdict on the network, and a two-hour sample on one
  evening is not a verdict on a line either.

## Tests

```bash
python -m unittest discover -s tests -v
```

32 tests, all offline against an in-memory database. They cover the DST rule at
both changeovers, the timestamp and schedule parsers, the haversine distance, the
wait formulas (including a randomised check that the penalty is never negative),
midnight-crossing delay matching, direction grouping across route variants, and
the event derivation — that a stationary bus produces no arrivals, that an
hour-long silence is not smeared into fabricated ones, that a parked bus is not
counted as a departure, and that a route returning to its own start stays one
trip. Three more cover the headway rule: two buses make a headway, one bus
lapping does not, and a single-vehicle loop reports no regularity figure.

## Layout

```
run.py              CLI: init | network | timetable | poll | events | analyze | status
watchlist.json      which lines to observe
oasa/client.py      throttled HTTP client, retries, API quirks
oasa/tz.py          Europe/Athens without tzdata
oasa/db.py          connection and schema
oasa/schema.sql     tables
oasa/network.py     lines, routes, stops, direction grouping
oasa/timetable.py   scheduled departures
oasa/poll.py        the collection loop
oasa/events.py      positions → stop arrivals → departures
oasa/analyze.py     headways, excess wait, adherence
oasa/report.py      terminal and HTML output
tests/              offline test suite
```
