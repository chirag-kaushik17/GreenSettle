"""Schema history of the GreenSettle database (one SQLite file for ledger and server).

The version is kept in SQLite's built-in `PRAGMA user_version`:

  version 1  Phase 2: users, ledger, jobs (minimal), settlements
  version 2  Phase 3: users.api_key_hash; scheduling columns on jobs;
             new tables telemetry, intensity_overrides, meta
  version 3  Phase 2 completion: users.decayed_through renamed to
             last_decay_at; settlements rebuilt WITHOUT saved_low_g and
             saved_high_g (the settlement rule does not return them; the
             dashboard can compute saved - k x unc from saved_g, saved_unc_g
             and the k stored in params_json); UNIQUE(job_id, kind) on the
             ledger so a job can be charged only once.
  version 4  the ledger table is rebuilt so its CHECK allows exactly the five
             row kinds of the spec (allowance, decay, fee, rebate, surcharge);
             'adjustment' is no longer accepted. If an old database contains
             an adjustment row the whole step fails and the file stays at v3.

Each step after version 1 runs inside one transaction, so a crash half-way
leaves the database at the previous version, never in between. Never edit a
released step: add a new one.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 4

SCHEMA_V1 = """
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

MIGRATION_V3 = """
BEGIN;
ALTER TABLE users RENAME COLUMN decayed_through TO last_decay_at;

CREATE TABLE settlements_v3 (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id),
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    settled_at      TEXT NOT NULL,
    energy_kwh      REAL NOT NULL,
    energy_unc_kwh  REAL NOT NULL,
    i_submit        REAL NOT NULL,
    i_submit_unc    REAL NOT NULL,
    i_run           REAL NOT NULL,
    i_run_unc       REAL NOT NULL,
    zone            TEXT NOT NULL CHECK (zone IN ('saving','dead_zone','backfire')),
    saved_g         REAL NOT NULL,
    saved_unc_g     REAL NOT NULL,
    fee_micro       INTEGER NOT NULL,
    rebate_micro    INTEGER NOT NULL,
    surcharge_micro INTEGER NOT NULL,
    net_micro       INTEGER NOT NULL,
    params_json     TEXT NOT NULL      -- copy of SettlementParams (includes k)
);
INSERT INTO settlements_v3
    SELECT job_id, user_id, settled_at, energy_kwh, energy_unc_kwh, i_submit, i_submit_unc,
           i_run, i_run_unc, zone, saved_g, saved_unc_g,
           fee_micro, rebate_micro, surcharge_micro, net_micro, params_json
    FROM settlements;
DROP TABLE settlements;
ALTER TABLE settlements_v3 RENAME TO settlements;

-- A job's fee, rebate and surcharge can each be written only once.
CREATE UNIQUE INDEX ledger_one_per_job ON ledger (job_id, kind) WHERE job_id IS NOT NULL;

PRAGMA user_version = 3;
COMMIT;
"""


MIGRATION_V4 = """
BEGIN;
DROP VIEW ledger_with_balance;
DROP TRIGGER ledger_no_update;
DROP TRIGGER ledger_no_delete;

CREATE TABLE ledger_v4 (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('allowance','decay','fee','rebate','surcharge')),
    amount_micro INTEGER NOT NULL CHECK (typeof(amount_micro) = 'integer'),
    job_id       TEXT NULL,
    period_key   TEXT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
INSERT INTO ledger_v4 SELECT entry_id, user_id, ts, kind, amount_micro, job_id, period_key, note FROM ledger;
DROP TABLE ledger;
ALTER TABLE ledger_v4 RENAME TO ledger;

CREATE UNIQUE INDEX ledger_one_per_period ON ledger (user_id, kind, period_key) WHERE period_key IS NOT NULL;
CREATE UNIQUE INDEX ledger_one_per_job ON ledger (job_id, kind) WHERE job_id IS NOT NULL;
CREATE INDEX ledger_by_user ON ledger (user_id, entry_id);

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE is not allowed');
END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE is not allowed');
END;

CREATE VIEW ledger_with_balance AS
SELECT
    entry_id, user_id, ts, kind, amount_micro, job_id, period_key, note,
    SUM(amount_micro) OVER (
        PARTITION BY user_id ORDER BY entry_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS balance_micro
FROM ledger;

PRAGMA user_version = 4;
COMMIT;
"""


def _run_step(conn: sqlite3.Connection, script: str) -> None:
    """Run one migration script; on any error undo it completely."""
    try:
        conn.executescript(script)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION and return the version."""
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database is version {version}, newer than this code ({SCHEMA_VERSION})")
    if version == 0:
        # Either a brand-new file or a Phase 2 database created before
        # user_version was used. SCHEMA_V1 only uses CREATE ... IF NOT EXISTS,
        # so running it is correct in both cases.
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
        version = 1
    if version == 1:
        _run_step(conn, MIGRATION_V2)
        version = 2
    if version == 2:
        _run_step(conn, MIGRATION_V3)
        version = 3
    if version == 3:
        _run_step(conn, MIGRATION_V4)
        version = 4
    return version
