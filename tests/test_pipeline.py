"""Tests for the parts that are easy to get quietly wrong.

Everything here runs offline against an in-memory database. The API is not
exercised -- these cover the parsing, geometry and statistics, which is where
the bugs that silently corrupt a number live.
"""

import os
import sys
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oasa import analyze, db, events, network, timetable
from oasa.client import as_float, as_int, parse_api_datetime
from oasa.tz import ATHENS, from_epoch, to_epoch


class TestTimezone(unittest.TestCase):
    def test_winter_is_eet_summer_is_eest(self):
        self.assertEqual(ATHENS.tzname(datetime(2026, 1, 15, 12)), "EET")
        self.assertEqual(ATHENS.tzname(datetime(2026, 7, 15, 12)), "EEST")

    def test_dst_switches_on_the_last_sunday_of_march(self):
        # 29 March 2026 is the last Sunday; the clocks go forward at 03:00 local.
        self.assertEqual(ATHENS.tzname(datetime(2026, 3, 29, 1)), "EET")
        self.assertEqual(ATHENS.tzname(datetime(2026, 3, 29, 4)), "EEST")

    def test_dst_ends_on_the_last_sunday_of_october(self):
        self.assertEqual(ATHENS.tzname(datetime(2026, 10, 25, 3)), "EEST")
        self.assertEqual(ATHENS.tzname(datetime(2026, 10, 25, 5)), "EET")

    def test_epoch_round_trip(self):
        for moment in (datetime(2026, 1, 15, 12, 30), datetime(2026, 7, 15, 23, 59)):
            self.assertEqual(from_epoch(to_epoch(moment)), moment)


class TestParsing(unittest.TestCase):
    def test_api_timestamp_with_padded_day_and_millis(self):
        self.assertEqual(parse_api_datetime("Sep  4 2026 10:13:21:000PM"),
                         datetime(2026, 9, 4, 22, 13, 21))

    def test_midnight_is_not_noon(self):
        self.assertEqual(parse_api_datetime("Sep 4 2026 12:05:00:000AM"),
                         datetime(2026, 9, 4, 0, 5, 0))

    def test_junk_returns_none_rather_than_raising(self):
        for value in (None, "", "not a date", "13/45/2026"):
            self.assertIsNone(parse_api_datetime(value))

    def test_numeric_coercion_tolerates_the_string_fields(self):
        self.assertEqual(as_float("37.98566"), 37.98566)
        self.assertIsNone(as_float(None))
        self.assertIsNone(as_float(""))
        self.assertEqual(as_int(" 42 "), 42)
        self.assertIsNone(as_int("x"))

    def test_schedule_time_becomes_minutes_after_midnight(self):
        self.assertEqual(timetable._minutes("1900-01-01 05:15:00"), 315)
        self.assertEqual(timetable._minutes("1900-01-01 00:00:00"), 0)
        self.assertIsNone(timetable._minutes(None))


class TestGeometry(unittest.TestCase):
    def test_haversine_against_a_known_distance(self):
        # Syntagma to Piraeus port, about 8.7 km apart.
        metres = events.haversine(37.9755, 23.7348, 37.9470, 23.6420)
        self.assertTrue(8000 < metres < 9500, metres)

    def test_zero_distance(self):
        self.assertAlmostEqual(events.haversine(37.9, 23.7, 37.9, 23.7), 0.0)


def _straight_route(conn, route_code="R1", line_code="L1", count=6, spacing_deg=0.003):
    """A synthetic line of stops running due east, roughly 265 m apart."""
    now = time.time()
    conn.execute("INSERT OR REPLACE INTO lines (line_code, line_id, ml_code, sdc_code, "
                 "descr, descr_en, fetched_at) VALUES (?,?,?,?,?,?,?)",
                 (line_code, "T1", "1", "86", "test", "test", now))
    conn.execute("INSERT OR REPLACE INTO routes (route_code, line_code, direction, descr, "
                 "descr_en, distance_m, fetched_at) VALUES (?,?,?,?,?,?,?)",
                 (route_code, line_code, "go", "test", "test", 1000.0, now))
    for index in range(count):
        stop_code = "S%d" % index
        conn.execute("INSERT OR REPLACE INTO stops (stop_code, stop_id, descr, descr_en, "
                     "lat, lng, fetched_at) VALUES (?,?,?,?,?,?,?)",
                     (stop_code, stop_code, stop_code, stop_code,
                      37.98, 23.73 + index * spacing_deg, now))
        conn.execute("INSERT OR REPLACE INTO route_stops (route_code, stop_code, stop_order) "
                     "VALUES (?,?,?)", (route_code, stop_code, index + 1))
    conn.commit()
    return [(37.98, 23.73 + i * spacing_deg) for i in range(count)]


class TestEventDerivation(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(db.connect(":memory:"))
        self.coords = _straight_route(self.conn)
        self.stops = events.route_geometry(self.conn, "R1")

    def tearDown(self):
        self.conn.close()

    def test_cumulative_distance_increases_along_the_route(self):
        cums = [s["cum_m"] for s in self.stops]
        self.assertEqual(cums, sorted(cums))
        self.assertAlmostEqual(cums[0], 0.0)

    def test_passing_stops_between_fixes_still_produces_arrivals(self):
        # Seen at stop 0, then at stop 3: stops 1 and 2 were passed unobserved.
        positions = [(1000.0, *self.coords[0]), (1300.0, *self.coords[3])]
        got = list(events._events_for_vehicle(self.stops, positions))
        orders = [index for index, _, _, _ in got]
        self.assertEqual(orders, [0, 1, 2, 3])

    def test_skipped_stops_are_interpolated_and_flagged(self):
        positions = [(1000.0, *self.coords[0]), (1300.0, *self.coords[3])]
        got = {index: (epoch, exact) for index, epoch, exact, _ in
               events._events_for_vehicle(self.stops, positions)}
        self.assertEqual(got[3][1], 1)                    # observed at the fix
        self.assertEqual(got[1][1], 0)                    # inferred
        self.assertTrue(1000.0 < got[1][0] < got[2][0] < got[3][0] <= 1300.0)

    def test_a_stationary_bus_produces_no_new_arrivals(self):
        positions = [(1000.0 + 30 * i, *self.coords[0]) for i in range(10)]
        got = list(events._events_for_vehicle(self.stops, positions))
        self.assertEqual([index for index, _, _, _ in got], [0])

    def test_returning_to_the_start_begins_a_new_trip(self):
        positions = [(1000.0, *self.coords[0]), (1300.0, *self.coords[5]),
                     (1600.0, *self.coords[0]), (1900.0, *self.coords[2])]
        trips = {trip for _, _, _, trip in events._events_for_vehicle(self.stops, positions)}
        self.assertEqual(trips, {0, 1})

    def test_a_long_silence_breaks_the_trip_instead_of_interpolating_across_it(self):
        positions = [(1000.0, *self.coords[0]), (1000.0 + 3600, *self.coords[3])]
        got = list(events._events_for_vehicle(self.stops, positions))
        # The hour-long gap must not be smeared into four fabricated arrivals.
        self.assertEqual([index for index, _, _, _ in got], [0])

    def test_a_route_returning_to_its_own_start_is_still_one_trip(self):
        # Athens route 4361 begins and ends at the same stop, so a bus finishing
        # its run is sitting next to stop 0 while legitimately at the last stop.
        # Matching it backwards there splits one trip into several and fabricates
        # a second set of arrivals on the way forward again.
        loop = list(self.coords) + [self.coords[0]]
        self.conn.execute(
            "INSERT OR REPLACE INTO routes (route_code, line_code, direction, descr, "
            "descr_en, distance_m, fetched_at) VALUES ('LOOP','L1','go','l','l',1.0,0)")
        for index, (lat, lng) in enumerate(loop):
            self.conn.execute(
                "INSERT OR REPLACE INTO stops (stop_code, stop_id, descr, descr_en, lat, "
                "lng, fetched_at) VALUES (?,?,?,?,?,?,0)",
                ("L%d" % index, "L%d" % index, "l", "l", lat, lng))
            self.conn.execute(
                "INSERT OR REPLACE INTO route_stops (route_code, stop_code, stop_order) "
                "VALUES ('LOOP',?,?)", ("L%d" % index, index + 1))
        self.conn.commit()

        loop_stops = events.route_geometry(self.conn, "LOOP")
        positions = [(1000.0 + 60 * i, lat, lng) for i, (lat, lng) in enumerate(loop)]
        got = list(events._events_for_vehicle(loop_stops, positions))
        self.assertEqual({trip for _, _, _, trip in got}, {0})
        self.assertEqual([index for index, _, _, _ in got], list(range(len(loop))))

    def test_a_fix_far_from_every_stop_is_ignored(self):
        positions = [(1000.0, 38.5, 24.5), (1300.0, *self.coords[2])]
        got = list(events._events_for_vehicle(self.stops, positions))
        self.assertEqual([index for index, _, _, _ in got], [2])

    def test_parked_bus_is_not_recorded_as_a_departure(self):
        for i in range(6):
            self.conn.execute(
                "INSERT INTO vehicle_positions (route_code, veh_no, cs_epoch, lat, lng, "
                "seen_epoch) VALUES (?,?,?,?,?,?)",
                ("R1", "V1", 1000.0 + 30 * i, self.coords[0][0], self.coords[0][1], 1.0))
        self.conn.commit()
        events.rebuild(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM trip_departures").fetchone()[0], 0)

    def test_a_bus_leaving_the_terminal_is_recorded_as_a_departure(self):
        track = [(0, 0), (1, 30), (2, 60), (3, 90), (4, 120), (5, 150)]
        for index, offset in track:
            self.conn.execute(
                "INSERT INTO vehicle_positions (route_code, veh_no, cs_epoch, lat, lng, "
                "seen_epoch) VALUES (?,?,?,?,?,?)",
                ("R1", "V2", 1000.0 + offset, self.coords[index][0], self.coords[index][1], 1.0))
        self.conn.commit()
        events.rebuild(self.conn)
        row = self.conn.execute("SELECT depart_epoch FROM trip_departures").fetchone()
        self.assertIsNotNone(row)
        # Departure is when it left stop 0, not when it was first seen sitting there.
        self.assertAlmostEqual(row[0], 1030.0, places=3)


class TestHeadwayMeasurement(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(db.connect(":memory:"))
        _straight_route(self.conn)

    def tearDown(self):
        self.conn.close()

    def _arrival(self, veh, order, epoch):
        self.conn.execute(
            "INSERT OR REPLACE INTO stop_events (route_code, stop_code, veh_no, trip_seq, "
            "stop_order, event_epoch, exact) VALUES ('R1',?,?,?,?,?,1)",
            ("S%d" % (order - 1), veh, int(epoch), order, epoch))
        self.conn.commit()

    def test_a_gap_between_two_buses_is_a_headway(self):
        self._arrival("V1", 2, 1000.0)
        self._arrival("V2", 2, 1600.0)
        gaps = analyze.observed_headways(self.conn, "R1")
        self.assertEqual(len(gaps), 1)
        _, minutes, distinct = gaps[0]
        self.assertAlmostEqual(minutes, 10.0)
        self.assertTrue(distinct)

    def test_the_same_bus_coming_round_again_is_not_a_headway(self):
        self._arrival("V1", 2, 1000.0)
        self._arrival("V1", 2, 1600.0)
        gaps = analyze.observed_headways(self.conn, "R1")
        self.assertEqual([distinct for _, _, distinct in gaps], [False])

    def test_a_single_bus_loop_reports_no_regularity_figure(self):
        # Every arrival is the same vehicle lapping, so there is no headway to
        # summarise and the route must not score as perfectly even service.
        for lap in range(4):
            self._arrival("V1", 2, 1000.0 + lap * 1500.0)
        summary = [r for r in analyze.route_summary(self.conn) if r["route_code"] == "R1"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["headways"], 0)
        self.assertEqual(summary[0]["same_vehicle_gaps"], 3)
        self.assertIsNone(summary[0]["cv"])
        self.assertIsNone(summary[0]["penalty"])


class TestDirectionAssignment(unittest.TestCase):
    def test_variants_from_the_same_terminal_share_a_direction(self):
        conn = db.init(db.connect(":memory:"))
        now = time.time()
        conn.execute("INSERT INTO lines (line_code, line_id, ml_code, sdc_code, descr, "
                     "descr_en, fetched_at) VALUES ('L','040','1','86','x','x',?)", (now,))
        places = {"A": (37.94, 23.64), "B": (37.97, 23.73), "A2": (37.9401, 23.6402)}
        for code, (lat, lng) in places.items():
            conn.execute("INSERT INTO stops (stop_code, stop_id, descr, descr_en, lat, lng, "
                         "fetched_at) VALUES (?,?,?,?,?,?,?)", (code, code, code, code, lat, lng, now))
        # RA and RC leave from the same terminal; RB is the opposite direction and
        # is listed between them, which is what defeats a positional rule.
        for route_code, origin in (("RA", "A"), ("RB", "B"), ("RC", "A2")):
            conn.execute("INSERT INTO routes (route_code, line_code, direction, descr, "
                         "descr_en, distance_m, fetched_at) VALUES (?,?,NULL,'x','x',1.0,?)",
                         (route_code, "L", now))
            conn.execute("INSERT INTO route_stops (route_code, stop_code, stop_order) "
                         "VALUES (?,?,1)", (route_code, origin))
        conn.commit()

        network.assign_directions(conn, ["RA", "RB", "RC"])
        got = dict(conn.execute("SELECT route_code, direction FROM routes").fetchall())
        self.assertEqual(got["RA"], "go")
        self.assertEqual(got["RC"], "go")
        self.assertEqual(got["RB"], "come")
        conn.close()


class TestWaitStatistics(unittest.TestCase):
    def test_even_headways_cost_no_excess_wait(self):
        actual, even, penalty = analyze.excess_wait([10.0] * 6)
        self.assertAlmostEqual(actual, 5.0)
        self.assertAlmostEqual(even, 5.0)
        self.assertAlmostEqual(penalty, 0.0)

    def test_bunched_headways_cost_the_passenger_time(self):
        # Same number of buses, same average gap, but clumped.
        actual, even, penalty = analyze.excess_wait([2.0, 18.0, 2.0, 18.0])
        self.assertAlmostEqual(actual, 8.2)
        self.assertAlmostEqual(even, 5.0)
        self.assertAlmostEqual(penalty, 3.2)

    def test_the_penalty_is_never_negative(self):
        import random
        random.seed(7)
        for _ in range(200):
            gaps = [random.uniform(0.1, 40.0) for _ in range(random.randint(2, 25))]
            _, _, penalty = analyze.excess_wait(gaps)
            self.assertGreaterEqual(penalty, -1e-9)

    def test_no_service_gives_no_number(self):
        self.assertEqual(analyze.excess_wait([]), (None, None, None))


class TestAdherenceMatching(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(db.connect(":memory:"))
        _straight_route(self.conn)

    def tearDown(self):
        self.conn.close()

    def _departure_at(self, local):
        self.conn.execute(
            "INSERT OR REPLACE INTO trip_departures (route_code, veh_no, trip_seq, "
            "depart_epoch) VALUES ('R1','V1',0,?)", (to_epoch(local),))
        self.conn.commit()

    def _schedule_at(self, minutes):
        self.conn.execute(
            "INSERT OR REPLACE INTO scheduled_trips (sde_code, line_code, direction, "
            "sdc_code, start_min, end_min, note, fetched_at) "
            "VALUES ('1','L1','go','86',?,?,NULL,0)", (minutes, minutes + 40))
        self.conn.commit()

    def test_a_late_departure_reports_a_positive_delay(self):
        self._schedule_at(8 * 60)                       # 08:00
        self._departure_at(datetime(2026, 9, 4, 8, 6))
        summary, unmatched = analyze.adherence(self.conn)
        self.assertEqual(unmatched, {})
        self.assertAlmostEqual(summary[0]["median_delay"], 6.0, places=1)
        self.assertEqual(summary[0]["on_time_pct"], 0.0)

    def test_a_departure_just_after_midnight_matches_the_late_night_trip(self):
        self._schedule_at(23 * 60 + 55)                 # 23:55
        self._departure_at(datetime(2026, 9, 4, 0, 5))  # 00:05
        summary, unmatched = analyze.adherence(self.conn)
        self.assertEqual(unmatched, {})
        # 10 minutes late, not 1435 minutes early.
        self.assertAlmostEqual(summary[0]["median_delay"], 10.0, places=1)

    def test_a_departure_with_no_nearby_scheduled_trip_is_left_unmatched(self):
        self._schedule_at(8 * 60)
        self._departure_at(datetime(2026, 9, 4, 14, 0))
        summary, unmatched = analyze.adherence(self.conn)
        self.assertEqual(summary, [])
        self.assertEqual(unmatched, {"T1": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
