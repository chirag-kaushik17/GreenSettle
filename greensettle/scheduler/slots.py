"""The scheduler's decision rule: WHEN should a submitted job start?

Pure function, no clock, no database. The planner (scheduler/planner.py)
calls choose_slot() once per job at submission, with a forecast that is
already frozen at the submission time.

Same principle as the Phase 2 settlement rule: do not act on a gain that is
smaller than the noise. Deferring a job for a forecast saving that is within
the forecast's own uncertainty would mostly add waiting time, not save carbon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

REASONS = ("deferred", "already_best", "gain_within_noise", "no_window")

# forecast_mean(start, end) -> (mean g/kWh, uncertainty g/kWh), already bound to the issue time.
ForecastMeanFn = Callable[[datetime, datetime], tuple[float, float]]


@dataclass(frozen=True)
class SlotRequest:
    submit_time: datetime  # t0, sim time
    max_delay_h: float  # how long the user can wait BEFORE STARTING (not a deadline for finishing)
    est_duration_s: float  # d, in SIM seconds


@dataclass(frozen=True)
class SchedulerParams:
    slot_seconds: float = 1800.0  # candidate spacing
    defer_z: float = 1.0  # z >= 0: only defer if the forecast gain exceeds z standard uncertainties


@dataclass(frozen=True)
class SlotChoice:
    start: datetime
    forecast_mean: float  # forecast g/kWh for the chosen slot
    forecast_unc: float
    forecast_now_mean: float  # forecast for starting at t0
    expected_saving: float  # forecast_now_mean - forecast_mean (g/kWh)
    deferred: bool
    reason: str  # "deferred" | "already_best" | "gain_within_noise" | "no_window"
    candidates: tuple  # (start, mean, unc) for every candidate, for the later dashboard


def choose_slot(
    request: SlotRequest,
    forecast_mean: ForecastMeanFn,
    params: SchedulerParams = SchedulerParams(),
) -> SlotChoice:
    """Pick the start time for one job: the greenest slot in its wait window,
    but only if the forecast gain is bigger than the forecast noise.

    Symbols: t0 = request.submit_time, d = request.est_duration_s (SIM seconds),
    z = params.defer_z, timedelta = datetime.timedelta.

    Validation (raise ValueError, before calling forecast_mean):
      - request.max_delay_h >= 0
      - request.est_duration_s > 0
      - params.slot_seconds > 0
      - params.defer_z >= 0

    Steps:
      1. Candidate starts:
           N   = floor(max_delay_h * 3600 / slot_seconds)
           s_i = t0 + timedelta(seconds=i * slot_seconds)   for i = 0, 1, ..., N
         Only starts INSIDE the wait window are candidates (s_N <= t0 + max_delay_h).
         The job is allowed to FINISH after the window.
      2. For each candidate i:
           (f_i, u_i) = forecast_mean(s_i, s_i + timedelta(seconds=d))
         Call forecast_mean exactly once per candidate, and for no other times.
      3. best = the index with the lowest f_i. Ties go to the EARLIEST index.
      4. saving = f_0 - f_best
         unc    = hypot(u_0, u_best)       (independent errors, quadrature)
      5. Decide (in this order):
           if N == 0:                start now, reason "no_window"
           elif best == 0:           start now, reason "already_best"
           elif saving <= z * unc:   start now, reason "gain_within_noise"
           else:                     start at s_best, reason "deferred"

    Return a SlotChoice:
      start             s_best if deferred, else t0
      forecast_mean     f of the CHOSEN start (f_best if deferred, else f_0)
      forecast_unc      u of the CHOSEN start (u_best if deferred, else u_0)
      forecast_now_mean f_0
      expected_saving   forecast_now_mean - forecast_mean
                        (so it is 0.0 whenever the job starts now)
      deferred          True only for reason "deferred"
      reason            as above
      candidates        tuple of (s_i, f_i, u_i) for i = 0..N, in order
    """
    raise NotImplementedError("Phase 3: write choose_slot yourself, see the docstring")
