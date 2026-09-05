-- Reference data (slow-moving; refreshed by `run.py network`)
CREATE TABLE IF NOT EXISTS lines (
    line_code   TEXT PRIMARY KEY,
    line_id     TEXT,            -- the number on the bus, e.g. "550"
    ml_code     TEXT,            -- master line, needed to fetch the timetable
    sdc_code    TEXT,            -- schedule-day code (weekday/Saturday/Sunday set)
    descr       TEXT,
    descr_en    TEXT,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_code  TEXT PRIMARY KEY,
    line_code   TEXT NOT NULL REFERENCES lines(line_code),
    direction   TEXT,            -- 'go' | 'come', positional (see README)
    descr       TEXT,
    descr_en    TEXT,
    distance_m  REAL,
    fetched_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routes_line ON routes(line_code);

CREATE TABLE IF NOT EXISTS stops (
    stop_code   TEXT PRIMARY KEY,
    stop_id     TEXT,
    descr       TEXT,
    descr_en    TEXT,
    lat         REAL,
    lng         REAL,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS route_stops (
    route_code  TEXT NOT NULL REFERENCES routes(route_code),
    stop_code   TEXT NOT NULL REFERENCES stops(stop_code),
    stop_order  INTEGER NOT NULL,
    PRIMARY KEY (route_code, stop_order)
);

-- Planned service (refreshed by `run.py timetable`)
CREATE TABLE IF NOT EXISTS scheduled_trips (
    -- A duty (sde_code) covers an outbound leg and a return leg, so the same
    -- sde_code appears once per direction. The key must include the direction.
    sde_code    TEXT NOT NULL,
    line_code   TEXT NOT NULL REFERENCES lines(line_code),
    direction   TEXT NOT NULL,   -- 'go' | 'come'
    sdc_code    TEXT,
    start_min   INTEGER,         -- minutes after midnight, Athens local
    end_min     INTEGER,
    note        TEXT,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (sde_code, direction)
);
CREATE INDEX IF NOT EXISTS idx_sched_line ON scheduled_trips(line_code, direction, start_min);

-- Observed service (appended by `run.py poll`)
CREATE TABLE IF NOT EXISTS vehicle_positions (
    route_code  TEXT NOT NULL,
    veh_no      TEXT NOT NULL,
    cs_epoch    REAL NOT NULL,   -- GPS timestamp reported by the vehicle
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    seen_epoch  REAL NOT NULL,   -- when we fetched it
    PRIMARY KEY (route_code, veh_no, cs_epoch)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_pos_route_time ON vehicle_positions(route_code, cs_epoch);

CREATE TABLE IF NOT EXISTS poll_cycles (
    id          INTEGER PRIMARY KEY,
    started_at  REAL NOT NULL,
    finished_at REAL,
    routes      INTEGER DEFAULT 0,
    rows_seen   INTEGER DEFAULT 0,
    rows_new    INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0
);

-- Derived (rebuilt by `run.py events`)
CREATE TABLE IF NOT EXISTS stop_events (
    route_code  TEXT NOT NULL,
    stop_code   TEXT NOT NULL,
    veh_no      TEXT NOT NULL,
    trip_seq    INTEGER NOT NULL, -- distinguishes repeat visits by the same bus
    stop_order  INTEGER NOT NULL,
    event_epoch REAL NOT NULL,
    exact       INTEGER NOT NULL, -- 1 = observed at the stop, 0 = interpolated
    PRIMARY KEY (route_code, veh_no, trip_seq, stop_order)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_events_stop ON stop_events(route_code, stop_code, event_epoch);

CREATE TABLE IF NOT EXISTS trip_departures (
    route_code  TEXT NOT NULL,
    veh_no      TEXT NOT NULL,
    trip_seq    INTEGER NOT NULL,
    depart_epoch REAL NOT NULL,
    PRIMARY KEY (route_code, veh_no, trip_seq)
) WITHOUT ROWID;
