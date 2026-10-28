"""Server database: schema migration and small helpers (meta, clock, overrides).

The server uses the SAME SQLite file as the Phase 2 ledger. Its schema
version is kept in SQLite's built-in `PRAGMA user_version`:

  version 1  Phase 2: users, ledger, jobs (minimal), settlements
  version 2  Phase 3: users.api_key_hash; extra job columns for scheduling;
             new tables telemetry, intensity_overrides, meta

Each migration step runs inside one transaction, so a crash half-way leaves
the database at the previous version, never in between.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from greensettle.intensity.timeline import OverrideTimeline
from greensettle.ledger import db as ledger_db
from greensettle.server.clock import Clock, RealTimeFn

SCHEMA_VERSION = 2

MIGRATION_V2 = """
BEGIN;
ALTER TABLE users ADD COLUMN api_key_hash TEXT;   -- SHA-256 of the user's API key (never the key)

-- jobs.declared_window_h (Phase 2) is reused as max_delay_h.
ALTER TABLE jobs ADD COLUMN job_type           TEXT;
ALTER TABLE jobs ADD COLUMN params_json        TEXT;
ALTER TABLE jobs ADD COLUMN est_duration_s     REAL;      -- REAL seconds, as declared by the user
ALTER TABLE jobs ADD COLUMN planned_start      TEXT;      -- sim time chosen by choose_slot
ALTER TABLE jobs ADD COLUMN latest_start       TEXT;      -- sim time: submitted_at + max_delay_h
ALTER TABLE jobs ADD COLUMN status             TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'));
ALTER TABLE jobs ADD COLUMN claimed_by         TEXT;      -- agent_id holding the lease
ALTER TABLE jobs ADD COLUMN attempts           INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN lease_expires_real REAL;      -- REAL epoch seconds
ALTER TABLE jobs ADD COLUMN started_real       REAL;
ALTER TABLE jobs ADD COLUMN ended_real         REAL;
ALTER TABLE jobs ADD COLUMN plan_json          TEXT;      -- the SlotChoice, for audit and the dashboard
ALTER TABLE jobs ADD COLUMN failure_reason     TEXT;

CREATE INDEX jobs_by_status ON jobs (status, user_id);

CREATE TABLE telemetry (
    ts_real  REAL NOT NULL,
    agent_id TEXT NOT NULL,
    watts    REAL NOT NULL
);

-- Demo overrides as an append-only TIMELINE (see intensity/timeline.py).
CREATE TABLE intensity_overrides (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    from_sim TEXT NOT NULL,
    state    TEXT NOT NULL CHECK (state IN ('green', 'dirty', 'off'))
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 2;
COMMIT;
"""


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION and return the version."""
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database is version {version}, newer than this code ({SCHEMA_VERSION})")
    if version == 0:
        # Phase 2 never set user_version; ledger_db.connect() has already
        # created its tables (CREATE ... IF NOT EXISTS), so this IS version 1.
        conn.execute("PRAGMA user_version = 1")
        version = 1
    if version == 1:
        conn.executescript(MIGRATION_V2)
        version = 2
    return version


def open_db(path: Path | str = ledger_db.DEFAULT_DB_PATH, timeout_s: float = 10.0) -> sqlite3.Connection:
    """Open the server database (one connection per thread) and migrate it."""
    conn = ledger_db.connect(path)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")  # wait instead of failing when locked
    migrate(conn)
    return conn


# --------------------------------------------------------------------------
# Sim-time strings
# --------------------------------------------------------------------------
def sim_ts(t: datetime) -> str:
    """Sim time as ISO-8601 UTC with a FIXED format (always microseconds)."""
    if t.tzinfo is None:
        raise ValueError("sim times must be timezone-aware")
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# meta table and clock persistence
# --------------------------------------------------------------------------
def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_clock(conn: sqlite3.Connection, clock: Clock) -> None:
    with conn:
        meta_set(conn, "clock_anchor_sim", sim_ts(clock.anchor_sim))
        meta_set(conn, "clock_anchor_real", repr(clock.anchor_real))
        meta_set(conn, "clock_scale", repr(clock.scale))


def load_clock(
    conn: sqlite3.Connection,
    scale: float,
    real_time: RealTimeFn,
    sim_start: datetime | None = None,
) -> Clock:
    """Start the server's clock.

    First start: sim time begins at `sim_start` (default: real UTC now).
    Restart: continue from the saved anchors (Clock.resume), re-anchored at
    the current sim time with the new scale. The anchors are saved again.
    """
    anchor_sim = meta_get(conn, "clock_anchor_sim")
    if anchor_sim is None:
        clock = Clock.start(sim_start, scale, real_time)
    else:
        clock = Clock.resume(
            parse_ts(anchor_sim),
            float(meta_get(conn, "clock_anchor_real") or 0),
            float(meta_get(conn, "clock_scale") or 1),
            scale,
            real_time,
        )
    save_clock(conn, clock)
    return clock


def set_clock_scale(conn: sqlite3.Connection, clock: Clock, new_scale: float) -> None:
    clock.set_scale(new_scale)
    save_clock(conn, clock)


# --------------------------------------------------------------------------
# Intensity override timeline
# --------------------------------------------------------------------------
def load_timeline(conn: sqlite3.Connection) -> OverrideTimeline:
    timeline = OverrideTimeline()
    for row in conn.execute("SELECT from_sim, state FROM intensity_overrides ORDER BY seq"):
        timeline.add(parse_ts(row["from_sim"]), row["state"])
    return timeline


def append_override(conn: sqlite3.Connection, sim_now: datetime, state: str) -> OverrideTimeline:
    """Add an override starting at the server's CURRENT sim time.

    Validated by OverrideTimeline.add first, so an override can never be
    placed before an existing one (the past is not rewritten).
    """
    timeline = load_timeline(conn)
    timeline.add(sim_now, state)  # raises ValueError if invalid
    with conn:
        conn.execute("INSERT INTO intensity_overrides (from_sim, state) VALUES (?, ?)", (sim_ts(sim_now), state))
    return timeline
