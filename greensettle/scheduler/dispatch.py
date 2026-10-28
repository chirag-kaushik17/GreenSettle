"""Which READY job runs next, and is it allowed to start? (pure functions)

A job is READY when its planned start has arrived (planned_start <= sim_now).
Among ready jobs the order is:

  1. OVERDUE jobs first (sim_now > latest_start). The wait window is a
     promise to the user: once it is used up, tier no longer matters
     (starvation guard), so a low-tier job cannot be pushed back forever.
  2. higher tier priority
  3. earlier planned_start
  4. earlier submission (submitted_at, then insertion order)

A job may start only if
  - fewer than max_concurrent_jobs jobs are running in total, AND
  - its user has fewer running jobs than their tier allows.

Tiers come from the user's ledger balance (Phase 2). Their definitions are
NOT specified yet, so callers pass a `tier_of(user_id)` function;
PLACEHOLDER_TIER is used until the real tiers exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Sequence

DEFAULT_MAX_CONCURRENT_JOBS = 2  # PLACEHOLDER: this laptop runs at most 2 jobs at once


@dataclass(frozen=True)
class TierInfo:
    name: str
    priority: int  # higher runs first
    max_concurrent: int  # running jobs allowed per user in this tier


# PLACEHOLDER until the Phase 2 tier rules are specified: everyone is equal.
PLACEHOLDER_TIER = TierInfo(name="default", priority=0, max_concurrent=1)

TierOf = Callable[[str], TierInfo]


def placeholder_tier_of(user_id: str) -> TierInfo:
    return PLACEHOLDER_TIER


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    user_id: str
    planned_start: datetime
    latest_start: datetime
    submitted_at: datetime
    seq: int  # insertion order (SQLite rowid): the final tie-breaker


def is_ready(job: QueuedJob, sim_now: datetime) -> bool:
    return job.planned_start <= sim_now


def is_overdue(job: QueuedJob, sim_now: datetime) -> bool:
    """Past the end of the wait window (strictly later than latest_start)."""
    return sim_now > job.latest_start


def dispatch_order(jobs: Sequence[QueuedJob], sim_now: datetime, tier_of: TierOf) -> list[QueuedJob]:
    """The READY jobs among `jobs`, best candidate first (see module docstring)."""

    def sort_key(job: QueuedJob) -> tuple:
        if is_overdue(job, sim_now):
            # Group 0: tier is ignored once the wait window is exhausted.
            return (0, 0, job.planned_start, job.submitted_at, job.seq)
        return (1, -tier_of(job.user_id).priority, job.planned_start, job.submitted_at, job.seq)

    return sorted((j for j in jobs if is_ready(j, sim_now)), key=sort_key)


def can_start(
    job: QueuedJob,
    running_total: int,
    running_by_user: Mapping[str, int],
    tier_of: TierOf,
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
) -> bool:
    if running_total >= max_concurrent_jobs:
        return False
    return running_by_user.get(job.user_id, 0) < tier_of(job.user_id).max_concurrent


def pick_next(
    jobs: Sequence[QueuedJob],
    sim_now: datetime,
    running_total: int,
    running_by_user: Mapping[str, int],
    tier_of: TierOf,
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
) -> QueuedJob | None:
    """The first job in dispatch order that is allowed to start, if any.

    A user who is at their own limit is skipped, so they do not block others.
    """
    for job in dispatch_order(jobs, sim_now, tier_of):
        if can_start(job, running_total, running_by_user, tier_of, max_concurrent_jobs):
            return job
    return None
