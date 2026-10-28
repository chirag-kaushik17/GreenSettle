"""Server database helpers: opening, sim-time strings, meta, clock, overrides.

The server uses the SAME SQLite file as the Phase 2 ledger. The schema
history (migrations) is in greensettle/ledger/migrations.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from greensettle.intensity.timeline import OverrideTimeline
from greensettle.ledger import db as ledger_db
from greensettle.ledger import migrations
from greensettle.server.clock import Clock, RealTimeFn

# The schema history lives with the ledger (one database file for both).
SCHEMA_VERSION = migrations.SCHEMA_VERSION
schema_version = migrations.schema_version
migrate = migrations.migrate


def open_db(path: Path | str = ledger_db.DEFAULT_DB_PATH, timeout_s: float = 10.0) -> sqlite3.Connection:
    """Open the server database (one connection per thread); connect() migrates it."""
    conn = ledger_db.connect(path)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")  # wait instead of failing when locked
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
