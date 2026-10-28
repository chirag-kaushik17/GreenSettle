"""The credit ledger: an append-only SQLite database.

Design rules (each one is enforced by the database itself, not just by
this Python code):
  - The ledger table is APPEND-ONLY: triggers make UPDATE and DELETE fail.
    Mistakes are corrected with a new 'adjustment' entry, like in accounting.
  - A balance is NEVER stored. It is always SUM(amount_micro) over a user's
    entries, so it cannot disagree with the history.
  - Amounts are INTEGER micro-credits (see credits.py); a CHECK constraint
    rejects floats.
  - The clock is injected: every function takes `now` (a timezone-aware
    datetime) instead of calling datetime.now(), so tests and experiments
    are deterministic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from greensettle.ledger.credits import to_micro

DEFAULT_DB_PATH = Path("data") / "greensettle.db"

ENTRY_KINDS = ("allowance", "fee", "rebate", "surcharge", "decay", "adjustment")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    decayed_through TEXT NOT NULL      -- decay has been applied up to this moment
);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,   -- also gives the order of entries
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('allowance','fee','rebate','surcharge','decay','adjustment')),
    amount_micro INTEGER NOT NULL CHECK (typeof(amount_micro) = 'integer'),
    job_id       TEXT NULL,
    period_key   TEXT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

-- At most one entry per (user, kind, period): makes grant_allowance idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_per_period
    ON ledger (user_id, kind, period_key) WHERE period_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ledger_by_user ON ledger (user_id, entry_id);

-- Append-only: any UPDATE or DELETE on the ledger is rejected.
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE is not allowed, add an adjustment entry');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE is not allowed, add an adjustment entry');
END;

-- Every entry with the user's balance right after it (a running total,
-- computed by a window function; nothing is stored).
CREATE VIEW IF NOT EXISTS ledger_with_balance AS
SELECT
    entry_id, user_id, ts, kind, amount_micro, job_id, period_key, note,
    SUM(amount_micro) OVER (
        PARTITION BY user_id ORDER BY entry_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS balance_micro
FROM ledger;

-- Minimal on purpose; Phase 3 (scheduler) will extend it.
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(user_id),
    submitted_at      TEXT NOT NULL,
    declared_window_h REAL,
    i_submit          REAL NOT NULL,
    i_submit_unc      REAL NOT NULL,
    i_submit_source   TEXT
);

-- One row per settled job: every input and output of the rule, for audit.
CREATE TABLE IF NOT EXISTS settlements (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id),
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    settled_at      TEXT NOT NULL,
    energy_kwh      REAL NOT NULL,
    energy_unc_kwh  REAL NOT NULL,
    i_submit        REAL NOT NULL,
    i_submit_unc    REAL NOT NULL,
    i_run           REAL NOT NULL,
    i_run_unc       REAL NOT NULL,
    saved_g         REAL NOT NULL,
    saved_unc_g     REAL NOT NULL,
    saved_low_g     REAL NOT NULL,
    saved_high_g    REAL NOT NULL,
    zone            TEXT NOT NULL CHECK (zone IN ('saving','dead_zone','backfire')),
    fee_micro       INTEGER NOT NULL,
    rebate_micro    INTEGER NOT NULL,
    surcharge_micro INTEGER NOT NULL,
    net_micro       INTEGER NOT NULL,
    params_json     TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Connection and time helpers
# --------------------------------------------------------------------------
def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the ledger database and make sure the schema exists.

    Pass ":memory:" for a throwaway in-memory database (used by the tests).
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores REFERENCES unless this is on
    conn.executescript(SCHEMA)
    return conn


def to_ts(now: datetime) -> str:
    """Store times as ISO-8601 in UTC. A naive datetime is rejected, because
    its meaning depends on the computer's time zone."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("`now` must be timezone-aware, e.g. datetime(2026, 1, 1, tzinfo=timezone.utc)")
    return now.astimezone(timezone.utc).isoformat()


def from_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# --------------------------------------------------------------------------
# Users and balances
# --------------------------------------------------------------------------
def create_user(conn: sqlite3.Connection, user_id: str, now: datetime) -> None:
    if not user_id:
        raise ValueError("user_id must not be empty")
    ts = to_ts(now)
    try:
        with conn:  # a transaction: committed on success, rolled back on error
            conn.execute(
                "INSERT INTO users (user_id, created_at, decayed_through) VALUES (?, ?, ?)",
                (user_id, ts, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"user {user_id!r} already exists") from exc


def user_exists(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None


def balance_micro(conn: sqlite3.Connection, user_id: str) -> int:
    """The balance is always computed from the history, never stored."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_micro), 0) AS total FROM ledger WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["total"])


def append_entry(
    conn: sqlite3.Connection,
    user_id: str,
    now: datetime,
    kind: str,
    amount_micro: int,
    job_id: str | None = None,
    period_key: str | None = None,
    note: str = "",
) -> int:
    """Append one ledger entry and return its entry_id. Does not commit;
    callers wrap it in `with conn:` so several entries commit together."""
    if kind not in ENTRY_KINDS:
        raise ValueError(f"kind must be one of {ENTRY_KINDS}, not {kind!r}")
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool):
        raise TypeError("amount_micro must be an int (micro-credits); convert with credits.to_micro()")
    if not user_exists(conn, user_id):
        raise ValueError(f"unknown user {user_id!r}")
    cur = conn.execute(
        "INSERT INTO ledger (user_id, ts, kind, amount_micro, job_id, period_key, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, to_ts(now), kind, amount_micro, job_id, period_key, note),
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# Allowance
# --------------------------------------------------------------------------
def grant_allowance(
    conn: sqlite3.Connection, user_id: str, now: datetime, amount: float, period_key: str
) -> int | None:
    """Give `amount` credits to a user for one period (e.g. "2026-W44").

    Idempotent: granting the same period twice does nothing the second time
    (returns None), thanks to the UNIQUE index on (user_id, kind, period_key).
    Returns the new entry_id when credits were granted.
    """
    if amount <= 0:
        raise ValueError("allowance amount must be positive")
    if not period_key:
        raise ValueError("period_key is required (it is what makes the grant idempotent)")
    try:
        with conn:
            return append_entry(conn, user_id, now, "allowance", to_micro(amount),
                                period_key=period_key, note=f"allowance for {period_key}")
    except sqlite3.IntegrityError:
        already = conn.execute(
            "SELECT 1 FROM ledger WHERE user_id = ? AND kind = 'allowance' AND period_key = ?",
            (user_id, period_key),
        ).fetchone()
        if already:
            return None
        raise
