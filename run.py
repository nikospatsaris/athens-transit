#!/usr/bin/env python3
"""Athens bus observatory -- command line entry point.

Typical first run:

    python run.py network      # lines, routes and stops for the watchlist
    python run.py timetable    # today's planned departures
    python run.py poll --minutes 90
    python run.py analyze --html report.html
"""

import argparse
import logging
import sys

from oasa import analyze, db, events, network, poll, report, timetable
from oasa.client import DEFAULT_UA, OasaClient


def _log(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=db.DEFAULT_DB, help="SQLite file (default: data/athens.sqlite)")
    parser.add_argument("--user-agent", default=DEFAULT_UA,
                        help="sent on every request; put a real contact in it")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and exit")
    sub.add_parser("network", help="fetch lines, routes and stops for the watchlist")
    sub.add_parser("timetable", help="fetch today's scheduled departures")

    polling = sub.add_parser("poll", help="record live vehicle positions")
    polling.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    polling.add_argument("--minutes", type=float, help="stop after this long")
    polling.add_argument("--cycles", type=int, help="stop after this many cycles")

    sub.add_parser("events", help="rebuild stop arrivals from stored positions")

    analysis = sub.add_parser("analyze", help="print the report")
    analysis.add_argument("--html", metavar="PATH", help="also write an HTML report")
    analysis.add_argument("--skip-events", action="store_true",
                          help="use the derived events as they stand")

    sub.add_parser("status", help="row counts per table")

    args = parser.parse_args(argv)
    _log(args.verbose)

    conn = db.init(db.connect(args.db))
    client = OasaClient(user_agent=args.user_agent)

    if args.command == "init":
        print("initialised %s" % args.db)

    elif args.command == "network":
        network.ingest(client, conn)

    elif args.command == "timetable":
        if not conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]:
            parser.error("no lines stored yet -- run `network` first")
        timetable.ingest(client, conn)

    elif args.command == "poll":
        if not conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]:
            parser.error("no routes stored yet -- run `network` first")
        duration = args.minutes * 60 if args.minutes else None
        totals = poll.run(client, conn, interval=args.interval,
                          duration=duration, cycles=args.cycles)
        print("%(cycles)d cycles, %(seen)d fixes fetched, %(new)d stored, %(errors)d errors"
              % totals)

    elif args.command == "events":
        events.rebuild(conn)

    elif args.command == "analyze":
        if not conn.execute("SELECT COUNT(*) FROM vehicle_positions").fetchone()[0]:
            parser.error("no positions stored yet -- run `poll` first")
        if not args.skip_events:
            events.rebuild(conn)
        print(report.text(conn))
        if args.html:
            with open(args.html, "w", encoding="utf-8") as handle:
                handle.write(report.html_page(conn))
            print("\nHTML report written to %s" % args.html)

    elif args.command == "status":
        for table, count in db.counts(conn).items():
            print("%-18s %7d" % (table, count))
        window = analyze.observation_window(conn)
        if window[0]:
            cover = analyze.coverage(conn)
            print("\nobserved %s -> %s (%.2f h)" % (
                cover["from"].strftime("%Y-%m-%d %H:%M"),
                cover["to"].strftime("%H:%M"), cover["hours"]))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
