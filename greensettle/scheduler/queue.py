"""The job queue in SQLite: submit, claim, leases, cancel, tick.

Job life cycle:

    queued --claim--> running --result--> done | failed
       |                 |
       |                 +--lease expired (1st time)--> queued (requeued)
       |                 +--lease expired (2nd time)--> failed
       +--cancel (owner, only while queued)--> cancelled

Every function takes the times it needs as arguments (sim_now for
scheduling, real_now for leases): no hidden clock, so tests are deterministic.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from greensettle.intensity.timeline import ActualProvider, ForecastProvider, mean_intensity
from greensettle.jobs import catalog
from greensettle.ledger import db as ledger_db
from greensettle.ledger.config import DEFAULT_CONFIG, LedgerConfig
from greensettle.ledger.db import immediate  # BEGIN IMMEDIATE transaction (see ledger/db.py)
from greensettle.scheduler.dispatch import (
    DEFAULT_MAX_CONCURRENT_JOBS,
    QueuedJob,
    TierOf,
    pick_next,
)
from greensettle.scheduler.planner import ChooseFn, Plan, choice_to_dict, plan_job
from greensettle.scheduler.slots import SchedulerParams, choose_slot
from greensettle.server.store import parse_ts, sim_ts

DEFAULT_MAX_QUEUED = 20  # PLACEHOLDER: queued jobs allowed per user
MAX_DELAY_H = 48.0  # PLACEHOLDER: longest wait window a user may declare
LEASE_FACTOR = 3.0  # lease = 3 x est_duration_s + 60 real seconds
LEASE_EXTRA_S = 60.0
MAX_ATTEMPTS = 2  # first lease expiry requeues, the second one fails the job


class SubmitError(ValueError):
    """The submission is invalid (bad parameters, unknown user, ...)."""


class QueueFullError(SubmitError):
    """The user already has the maximum number of queued jobs."""


class JobNotFound(LookupError):
    """No such job, or it belongs to someone else (we do not say which)."""


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    plan: Plan
    i_submit_provisional: tuple[float, float]


def _validate_delay(max_delay_h: Any) -> float:
    if isinstance(max_delay_h, bool) or not isinstance(max_delay_h, (int, float)):
        raise SubmitError("max_delay_h must be a number")
    value = float(max_delay_h)
    if not math.isfinite(value) or not 0 <= value <= MAX_DELAY_H:
        raise SubmitError(f"max_delay_h must be in [0, {MAX_DELAY_H:g}]")
    return value


def submit_job(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    job_type: Any,
    params: Any,
    est_duration_s: Any,
    max_delay_h: Any,
    sim_now: datetime,
    time_scale: float,
    forecast: ForecastProvider,
    actual: ActualProvider,
    sched_params: SchedulerParams = SchedulerParams(),
    max_queued: int = DEFAULT_MAX_QUEUED,
    choose: ChooseFn = choose_slot,
    job_id: str | None = None,
) -> SubmitResult:
    """Validate, plan once with the forecast issued NOW, and store a queued job.

    jobs.i_submit is NOT NULL (Phase 2 schema), so we store a PROVISIONAL
    value: the actual intensity over [t0, t0 + d] as known at submission.
    Settlement recomputes the counterfactual from actual data at result
    time, because an override added later could still change that interval.
    """
    try:
        clean_params = catalog.validate(job_type, params)
        est = catalog.validate_est_duration(est_duration_s)
    except catalog.CatalogError as exc:
        raise SubmitError(str(exc)) from exc
    delay = _validate_delay(max_delay_h)
    if not ledger_db.user_exists(conn, user_id):
        raise SubmitError(f"unknown user {user_id!r}")

    # Plan BEFORE writing anything: if planning fails, nothing is stored.
    plan = plan_job(sim_now, delay, est, time_scale, forecast, sched_params, choose)
    i_sub = mean_intensity(actual, sim_now, sim_now + timedelta(seconds=plan.est_duration_sim_s))
    job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"

    with immediate(conn):  # count + insert atomically, so the cap cannot be raced
        queued = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'queued'", (user_id,)
        ).fetchone()[0]
        if queued >= max_queued:
            raise QueueFullError(f"user {user_id!r} already has {queued} queued jobs (limit {max_queued})")
        conn.execute(
            """INSERT INTO jobs (job_id, user_id, submitted_at, declared_window_h,
                                 i_submit, i_submit_unc, i_submit_source,
                                 job_type, params_json, est_duration_s,
                                 planned_start, latest_start, status, plan_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (
                job_id, user_id, sim_ts(sim_now), delay,
                i_sub[0], i_sub[1], "actual-at-submit (provisional; settlement recomputes)",
                job_type, json.dumps(clean_params), est,
                sim_ts(plan.choice.start), sim_ts(plan.latest_start), json.dumps(choice_to_dict(plan.choice)),
            ),
        )
    return SubmitResult(job_id=job_id, plan=plan, i_submit_provisional=i_sub)


# --------------------------------------------------------------------------
# Reading jobs
# --------------------------------------------------------------------------
def get_job(conn: sqlite3.Connection, job_id: str, user_id: str | None = None) -> dict[str, Any]:
    """One job as a dict. If user_id is given, only the owner may see it."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise JobNotFound(job_id)
    job = dict(row)
    job["params"] = json.loads(job.pop("params_json") or "{}")
    job["plan"] = json.loads(job.pop("plan_json") or "null")
    job["max_delay_h"] = job.pop("declared_window_h")
    return job


def _queued_jobs(conn: sqlite3.Connection) -> list[QueuedJob]:
    rows = conn.execute(
        "SELECT rowid, job_id, user_id, planned_start, latest_start, submitted_at FROM jobs WHERE status = 'queued'"
    ).fetchall()
    return [
        QueuedJob(
            job_id=r["job_id"], user_id=r["user_id"],
            planned_start=parse_ts(r["planned_start"]), latest_start=parse_ts(r["latest_start"]),
            submitted_at=parse_ts(r["submitted_at"]), seq=r["rowid"],
        )
        for r in rows
    ]


def _running_counts(conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    by_user = {
        r["user_id"]: r["n"]
        for r in conn.execute("SELECT user_id, COUNT(*) AS n FROM jobs WHERE status = 'running' GROUP BY user_id")
    }
    return sum(by_user.values()), by_user


# --------------------------------------------------------------------------
# Claim (worker asks for work)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Claim:
    job_id: str
    job_type: str
    params: dict[str, int]
    est_duration_s: float
    lease_expires_real: float


def lease_seconds(est_duration_s: float) -> float:
    return LEASE_FACTOR * est_duration_s + LEASE_EXTRA_S


def claim_job(
    conn: sqlite3.Connection,
    agent_id: str,
    sim_now: datetime,
    real_now: float,
    tier_of: TierOf | None = None,
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
    config: LedgerConfig = DEFAULT_CONFIG,
) -> Claim | None:
    """Atomically pick the next startable job, mark it running, give it a lease.

    BEGIN IMMEDIATE means two workers claiming at the same moment cannot both
    read "job X is queued" and both take it: the second one waits until the
    first has committed, and then sees X as running.

    tier_of defaults to the user's tier from their ledger balance (decay has
    already been applied by the tick). Tests may pass their own.
    Returns None when nothing is ready or the concurrency limits are reached
    (the API turns that into HTTP 204).
    """
    if not agent_id:
        raise ValueError("agent_id is required")
    if tier_of is None:
        def tier_of(user_id: str):  # noqa: E306 (reads the balance inside the same transaction)
            return ledger_db.tier_of_user(conn, user_id, config)
    with immediate(conn):
        running_total, running_by_user = _running_counts(conn)
        job = pick_next(_queued_jobs(conn), sim_now, running_total, running_by_user, tier_of, max_concurrent_jobs)
        if job is None:
            return None
        row = conn.execute(
            "SELECT job_type, params_json, est_duration_s FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        lease_expires = real_now + lease_seconds(row["est_duration_s"])
        conn.execute(
            """UPDATE jobs SET status = 'running', claimed_by = ?, attempts = attempts + 1,
                               lease_expires_real = ?
               WHERE job_id = ? AND status = 'queued'""",
            (agent_id, lease_expires, job.job_id),
        )
    return Claim(job.job_id, row["job_type"], json.loads(row["params_json"]), row["est_duration_s"], lease_expires)


# --------------------------------------------------------------------------
# Leases and the scheduler tick
# --------------------------------------------------------------------------
def expire_leases(conn: sqlite3.Connection, real_now: float) -> list[tuple[str, str]]:
    """Running jobs whose lease ran out: requeue the first time, fail the second.

    A lease runs out when the worker died, lost the network, or the job hung.
    Returns [(job_id, new_status), ...].
    """
    changed: list[tuple[str, str]] = []
    with immediate(conn):
        expired = conn.execute(
            "SELECT job_id, attempts FROM jobs WHERE status = 'running' AND lease_expires_real < ?", (real_now,)
        ).fetchall()
        for row in expired:
            if row["attempts"] >= MAX_ATTEMPTS:
                conn.execute(
                    """UPDATE jobs SET status = 'failed', lease_expires_real = NULL,
                                       failure_reason = 'lease expired twice'
                       WHERE job_id = ?""",
                    (row["job_id"],),
                )
                changed.append((row["job_id"], "failed"))
            else:
                conn.execute(
                    "UPDATE jobs SET status = 'queued', claimed_by = NULL, lease_expires_real = NULL WHERE job_id = ?",
                    (row["job_id"],),
                )
                changed.append((row["job_id"], "queued"))
    return changed


@dataclass(frozen=True)
class TickReport:
    sim_now: datetime
    decayed_micro: dict[str, int] = field(default_factory=dict)  # user -> micro-credits removed
    lease_changes: list[tuple[str, str]] = field(default_factory=list)


def tick(
    conn: sqlite3.Connection, sim_now: datetime, real_now: float, config: LedgerConfig = DEFAULT_CONFIG
) -> TickReport:
    """One scheduler tick. The server calls it every real second.

    1. Apply decay (at sim_now) for every user who has a READY queued job, so
       the tier used to order the queue reflects decay. Decay is otherwise
       lazy (before balance-changing writes); this is the one extra place.
    2. Expire leases (requeue once, fail the second time).
    Nothing else changes jobs.
    """
    ready_users = sorted({
        j.user_id for j in _queued_jobs(conn) if j.planned_start <= sim_now
    })
    decayed = {user: ledger_db.apply_decay(conn, user, sim_now, config) for user in ready_users}
    return TickReport(sim_now=sim_now, decayed_micro=decayed, lease_changes=expire_leases(conn, real_now))


# --------------------------------------------------------------------------
# Cancel
# --------------------------------------------------------------------------
def cancel_job(conn: sqlite3.Connection, job_id: str, user_id: str) -> None:
    """Owner-only, and only while the job is still queued."""
    with immediate(conn):
        row = conn.execute("SELECT user_id, status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or row["user_id"] != user_id:
            raise JobNotFound(job_id)
        if row["status"] != "queued":
            raise ValueError(f"job {job_id} is {row['status']}; only queued jobs can be cancelled")
        conn.execute("UPDATE jobs SET status = 'cancelled' WHERE job_id = ?", (job_id,))
