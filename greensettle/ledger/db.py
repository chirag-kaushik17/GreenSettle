"""The credit ledger: an append-only SQLite database.

Design rules (each one is enforced by the database itself, not just by
this Python code):
  - The ledger table is APPEND-ONLY: triggers make UPDATE and DELETE fail.
    History is never rewritten; every change is a new row.
  - A balance is NEVER stored. It is always SUM(amount_micro) over a user's
    entries, so it cannot disagree with the history.
  - Amounts are INTEGER micro-credits (see credits.py); a CHECK constraint
    rejects floats.
  - The clock is injected: every function takes `now` (a timezone-aware
    datetime) instead of calling datetime.now(), so tests and experiments
    are deterministic.
  - Every operation is ONE atomic transaction started with BEGIN IMMEDIATE.
  - Decay is LAZY: it is applied to a user right before every write that
    changes their balance (allowance, settlement), never by a background job.
  - Balances may go negative (debt). The user is then in the red tier, and
    the next monthly allowance helps them recover.
"""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from greensettle.ledger import migrations
from greensettle.ledger.config import DEFAULT_CONFIG, LedgerConfig, TierInfo, tier_for
from greensettle.ledger.credits import MICRO_PER_CREDIT, to_micro

DEFAULT_DB_PATH = Path("data") / "greensettle.db"

ENTRY_KINDS = ("allowance", "decay", "fee", "rebate", "surcharge")



# --------------------------------------------------------------------------
# Connection, transactions and time helpers
# --------------------------------------------------------------------------
def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to date.

    Pass ":memory:" for a throwaway in-memory database (used by the tests).
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores REFERENCES unless this is on
    migrations.migrate(conn)  # create or upgrade the schema (see migrations.py)
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One atomic write transaction that takes SQLite's write lock straight away.

    Everything inside either commits together or not at all. Taking the lock
    at the START (IMMEDIATE) means two writers can never both read a value,
    then both write based on it: the second waits for the first to finish.
    """
    if conn.in_transaction:
        raise RuntimeError("a transaction is already open on this connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


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
    """New users start with balance 0 and last_decay_at = creation time."""
    if not user_id:
        raise ValueError("user_id must not be empty")
    ts = to_ts(now)
    try:
        with immediate(conn):
            conn.execute(
                "INSERT INTO users (user_id, created_at, last_decay_at) VALUES (?, ?, ?)",
                (user_id, ts, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"user {user_id!r} already exists") from exc


def user_exists(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None


def last_decay_at(conn: sqlite3.Connection, user_id: str) -> datetime:
    row = conn.execute("SELECT last_decay_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown user {user_id!r}")
    return from_ts(row["last_decay_at"])


def balance_micro(conn: sqlite3.Connection, user_id: str) -> int:
    """The balance is always computed from the history, never stored."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_micro), 0) AS total FROM ledger WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["total"])


def balance_credits(conn: sqlite3.Connection, user_id: str) -> float:
    return balance_micro(conn, user_id) / MICRO_PER_CREDIT


def tier_of_user(conn: sqlite3.Connection, user_id: str, config: LedgerConfig = DEFAULT_CONFIG) -> TierInfo:
    """The user's tier from their CURRENT ledger balance.

    It does not apply decay itself (reads never write); the scheduler tick
    applies decay first, so the balance it sees is already decayed.
    """
    return tier_for(balance_credits(conn, user_id), config.tiers)


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
    """Append one ledger entry and return its entry_id.

    Does NOT open a transaction: callers wrap it in `immediate(conn)` so
    several entries (and the decay before them) commit together.
    """
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
# Decay (lazy)
# --------------------------------------------------------------------------
def decay_amount_micro(balance: int, elapsed_days: float, half_life_days: float) -> int:
    """How many micro-credits a balance loses over elapsed_days. Pure function.

        decay = floor(balance x (1 - 0.5 ** (elapsed_days / half_life_days)))

    After one half-life half of a positive balance is gone. Only positive
    balances decay: debt (a negative balance) neither shrinks nor grows.
    floor() means the user never loses a fraction of a micro-credit more
    than the formula says.
    """
    if balance <= 0 or elapsed_days <= 0:
        return 0
    return math.floor(balance * (1 - 0.5 ** (elapsed_days / half_life_days)))


def decay_in_transaction(
    conn: sqlite3.Connection, user_id: str, now: datetime, config: LedgerConfig = DEFAULT_CONFIG
) -> int:
    """Apply decay inside an ALREADY OPEN transaction. Returns the micro-credits removed.

    - elapsed <= 0: do nothing at all (the clock never goes backwards).
    - otherwise: append a negative `decay` row if it is at least 1 micro-credit,
      and set last_decay_at = now in every case. Rounding losses below one
      micro-credit are accepted.
    """
    elapsed_days = (now - last_decay_at(conn, user_id)).total_seconds() / 86_400
    if elapsed_days <= 0:
        return 0
    decay = decay_amount_micro(balance_micro(conn, user_id), elapsed_days, config.half_life_days)
    if decay >= 1:
        append_entry(conn, user_id, now, "decay", -decay, note=f"decay over {elapsed_days:.3f} days")
    conn.execute("UPDATE users SET last_decay_at = ? WHERE user_id = ?", (to_ts(now), user_id))
    return decay


def apply_decay(conn: sqlite3.Connection, user_id: str, now: datetime, config: LedgerConfig = DEFAULT_CONFIG) -> int:
    """Apply decay as its own atomic transaction (used by the scheduler tick)."""
    with immediate(conn):
        return decay_in_transaction(conn, user_id, now, config)


# --------------------------------------------------------------------------
# Allowance
# --------------------------------------------------------------------------
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_period(now: datetime) -> str:
    """The calendar-month period ("YYYY-MM", in UTC) that `now` falls in."""
    to_ts(now)  # rejects naive datetimes
    return now.astimezone(timezone.utc).strftime("%Y-%m")


@dataclass(frozen=True)
class AllowanceResult:
    granted: bool  # False means this period was already granted (nothing written)
    period: str
    amount_micro: int  # 0 when not granted
    decay_micro: int  # decay applied just before the grant
    entry_id: int | None
    message: str


def grant_allowance(
    conn: sqlite3.Connection,
    user_id: str,
    now: datetime,
    amount: float | None = None,
    period: str | None = None,
    config: LedgerConfig = DEFAULT_CONFIG,
) -> AllowanceResult:
    """Give a user their allowance for one calendar month, in ONE transaction.

    Defaults: amount = config.allowance_credits (100), period = the month of `now`.
    Idempotent: if the period was already granted, NOTHING is written (not
    even decay) and the result says "already granted". The UNIQUE index on
    (user_id, kind, period_key) enforces this in the database as well.
    Otherwise decay is applied first, then the allowance row is appended.
    """
    period = period or month_period(now)
    if not _PERIOD.match(period):
        raise ValueError(f"period must look like YYYY-MM, not {period!r}")
    amount = config.allowance_credits if amount is None else amount
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("allowance amount must be a positive number of credits")
    amount_micro = to_micro(amount)
    with immediate(conn):
        if not user_exists(conn, user_id):
            raise ValueError(f"unknown user {user_id!r}")
        already = conn.execute(
            "SELECT entry_id FROM ledger WHERE user_id = ? AND kind = 'allowance' AND period_key = ?",
            (user_id, period),
        ).fetchone()
        if already:
            return AllowanceResult(False, period, 0, 0, None, f"already granted for {period}")
        decay = decay_in_transaction(conn, user_id, now, config)
        entry_id = append_entry(conn, user_id, now, "allowance", amount_micro,
                                period_key=period, note=f"allowance for {period}")
    return AllowanceResult(True, period, amount_micro, decay, entry_id, f"granted {amount:g} credits for {period}")
