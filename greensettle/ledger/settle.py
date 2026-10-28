"""settle_job: run the settlement rule for one finished job and book the result.

    measured energy + intensities
              |
              v
    compute_settlement()      (greensettle/settlement/rules.py, pure arithmetic)
              |
              v
    settlement_to_micro()     (rounding never favours the user, see credits.py)
              |
              v
    ONE transaction: decay -> settlements row -> fee / rebate / surcharge rows

The ledger rows are signed from the user's point of view:
    fee        negative (the user pays)
    rebate     positive (the user gets credits back)
    surcharge  negative (extra charge for a proven backfire; 0 by default)
Zero-valued rows are skipped.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from greensettle.ledger import db
from greensettle.ledger.config import DEFAULT_CONFIG, LedgerConfig, tier_for
from greensettle.ledger.credits import MICRO_PER_CREDIT, settlement_to_micro
from greensettle.settlement.rules import compute_settlement


@dataclass(frozen=True)
class SettleOutcome:
    job_id: str
    user_id: str
    settled_at: str
    zone: str
    saved_g: float
    saved_unc_g: float
    fee_micro: int
    rebate_micro: int
    surcharge_micro: int
    net_micro: int  # fee + surcharge - rebate (what the job cost the user)
    params: dict[str, Any]  # copy of the SettlementParams used (includes k)
    balance_micro_after: int  # the user's balance now
    tier_after: str
    already_settled: bool  # True: this call wrote nothing and returned the stored result
    decay_micro: int  # decay applied just before booking (0 if already settled)


def _outcome(conn: sqlite3.Connection, row: sqlite3.Row, config: LedgerConfig,
             already_settled: bool, decay_micro: int) -> SettleOutcome:
    balance = db.balance_micro(conn, row["user_id"])
    return SettleOutcome(
        job_id=row["job_id"], user_id=row["user_id"], settled_at=row["settled_at"], zone=row["zone"],
        saved_g=row["saved_g"], saved_unc_g=row["saved_unc_g"],
        fee_micro=row["fee_micro"], rebate_micro=row["rebate_micro"],
        surcharge_micro=row["surcharge_micro"], net_micro=row["net_micro"],
        params=json.loads(row["params_json"]),
        balance_micro_after=balance, tier_after=tier_for(balance / MICRO_PER_CREDIT, config.tiers).name,
        already_settled=already_settled, decay_micro=decay_micro,
    )


def settle_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    user_id: str,
    now: datetime,
    energy_kwh: float,
    energy_unc_kwh: float,
    i_submit: float,
    i_submit_unc: float,
    i_run: float,
    i_run_unc: float,
    config: LedgerConfig = DEFAULT_CONFIG,
) -> SettleOutcome:
    """Settle one job, atomically and idempotently.

    - The job must already exist in `jobs` and belong to `user_id`.
    - Idempotent: if the job was settled before, NOTHING is written and the
      stored settlement is returned (already_settled=True). The database
      also enforces this: settlements.job_id is a PRIMARY KEY and the ledger
      has UNIQUE(job_id, kind).
    - Atomic: decay, the settlements row and the ledger rows are written in
      one BEGIN IMMEDIATE transaction; any error writes nothing.
    - `now` is injected (sim time on the server); it timestamps every row.
    """
    params = config.settlement
    with db.immediate(conn):
        job = conn.execute("SELECT user_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise ValueError(f"unknown job {job_id!r}: it must be recorded in `jobs` before settling")
        if job["user_id"] != user_id:
            raise ValueError(f"job {job_id!r} does not belong to {user_id!r}")
        stored = conn.execute("SELECT * FROM settlements WHERE job_id = ?", (job_id,)).fetchone()
        if stored is not None:
            return _outcome(conn, stored, config, already_settled=True, decay_micro=0)

        result = compute_settlement(energy_kwh, energy_unc_kwh, i_submit, i_submit_unc, i_run, i_run_unc, params)
        money = settlement_to_micro(result, params)

        decay = db.decay_in_transaction(conn, user_id, now, config)  # decay BEFORE the balance changes
        conn.execute(
            """INSERT INTO settlements (job_id, user_id, settled_at, energy_kwh, energy_unc_kwh,
                                        i_submit, i_submit_unc, i_run, i_run_unc,
                                        zone, saved_g, saved_unc_g,
                                        fee_micro, rebate_micro, surcharge_micro, net_micro, params_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, user_id, db.to_ts(now), energy_kwh, energy_unc_kwh, i_submit, i_submit_unc, i_run, i_run_unc,
             result.zone, result.saved_g, result.saved_unc_g,
             money.fee, money.rebate, money.surcharge, money.net, json.dumps(asdict(params))),
        )
        for kind, amount in (("fee", -money.fee), ("rebate", money.rebate), ("surcharge", -money.surcharge)):
            if amount != 0:
                db.append_entry(conn, user_id, now, kind, amount, job_id=job_id, note=f"{result.zone}")
        row = conn.execute("SELECT * FROM settlements WHERE job_id = ?", (job_id,)).fetchone()
        return _outcome(conn, row, config, already_settled=False, decay_micro=decay)


def record_offline_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    user_id: str,
    submitted_at: datetime,
    i_submit: float,
    i_submit_unc: float,
    source: str,
) -> None:
    """Phase 2 CLI only: create the `jobs` row for a measurement settled offline.

    (On the Phase 3 server the row is created when the job is submitted.)
    Safe to call twice for the same job and user; refuses another user's job.
    """
    with db.immediate(conn):
        if not db.user_exists(conn, user_id):
            raise ValueError(f"unknown user {user_id!r}")
        row = conn.execute("SELECT user_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is not None:
            if row["user_id"] != user_id:
                raise ValueError(f"job {job_id!r} belongs to another user")
            return
        conn.execute(
            """INSERT INTO jobs (job_id, user_id, submitted_at, i_submit, i_submit_unc, i_submit_source, status)
               VALUES (?, ?, ?, ?, ?, ?, 'done')""",
            (job_id, user_id, db.to_ts(submitted_at), i_submit, i_submit_unc, source),
        )
