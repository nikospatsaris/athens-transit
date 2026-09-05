"""SQLite storage. One file, no server, safe to delete and rebuild."""

import os
import sqlite3

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "athens.sqlite")


def connect(path=DEFAULT_DB):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL lets `analyze` read while `poll` is still writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn):
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.commit()
    return conn


def counts(conn):
    tables = ["lines", "routes", "stops", "route_stops", "scheduled_trips",
              "vehicle_positions", "stop_events", "trip_departures", "poll_cycles"]
    out = {}
    for table in tables:
        out[table] = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return out
