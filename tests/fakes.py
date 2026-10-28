"""Test doubles shared by the Phase 3 tests.

fake_choose is NOT the slot rule. It is a deliberately trivial stand-in
(start 1 hour after submission if the window allows, else now) that lets
the queue, planner and API be tested before choose_slot is written. It never
looks at the forecast values to decide anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from greensettle.scheduler.slots import SchedulerParams, SlotChoice, SlotRequest


class FakeRealTime:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def fake_choose(request: SlotRequest, forecast_mean, params: SchedulerParams) -> SlotChoice:
    t0 = request.submit_time
    end = t0 + timedelta(seconds=request.est_duration_s)
    now_mean, now_unc = forecast_mean(t0, end)
    if request.max_delay_h >= 1:
        start = t0 + timedelta(hours=1)
        mean, unc = forecast_mean(start, start + timedelta(seconds=request.est_duration_s))
        return SlotChoice(start, mean, unc, now_mean, now_mean - mean, True, "deferred",
                          ((t0, now_mean, now_unc), (start, mean, unc)))
    return SlotChoice(t0, now_mean, now_unc, now_mean, 0.0, False, "no_window", ((t0, now_mean, now_unc),))


def start_now_choose(request: SlotRequest, forecast_mean, params: SchedulerParams) -> SlotChoice:
    t0 = request.submit_time
    mean, unc = forecast_mean(t0, t0 + timedelta(seconds=request.est_duration_s))
    return SlotChoice(t0, mean, unc, mean, 0.0, False, "already_best", ((t0, mean, unc),))


def at_hours(t0: datetime, hours: float) -> datetime:
    return t0 + timedelta(hours=hours)
