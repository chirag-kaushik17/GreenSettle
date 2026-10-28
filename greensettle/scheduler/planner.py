"""Planning a job at submission: call choose_slot ONCE with the forecast issued now.

LIMIT (by design, see docs): a job is planned once and never re-planned,
even if a later forecast would have chosen differently.

`choose` defaults to the real choose_slot. Tests inject a trivial fake so
everything around it can be verified before choose_slot is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from greensettle.intensity.timeline import ForecastMeanFn, ForecastProvider
from greensettle.scheduler.slots import SchedulerParams, SlotChoice, SlotRequest, choose_slot

ChooseFn = Callable[[SlotRequest, ForecastMeanFn, SchedulerParams], SlotChoice]


@dataclass(frozen=True)
class Plan:
    choice: SlotChoice
    latest_start: datetime  # submit_time + max_delay_h (end of the wait window)
    est_duration_sim_s: float  # the user's REAL estimate converted to SIM seconds


def plan_job(
    submit_time: datetime,
    max_delay_h: float,
    est_duration_real_s: float,
    time_scale: float,
    forecast: ForecastProvider,
    params: SchedulerParams = SchedulerParams(),
    choose: ChooseFn = choose_slot,
) -> Plan:
    """Build the SlotRequest in SIM units and ask choose_slot for a start time.

    est_duration_s comes from the user in REAL seconds (how long it runs on
    the laptop). At --time-scale 600 a 25 s job spans 25 * 600 = 15,000 sim
    seconds (about 4 sim hours) of the grid curve, so it is converted here.
    """
    d_sim = est_duration_real_s * time_scale
    request = SlotRequest(submit_time=submit_time, max_delay_h=max_delay_h, est_duration_s=d_sim)
    choice = choose(request, forecast.bound(issued_at=submit_time), params)
    return Plan(choice=choice, latest_start=submit_time + timedelta(hours=max_delay_h), est_duration_sim_s=d_sim)


def choice_to_dict(choice: SlotChoice) -> dict[str, Any]:
    """JSON-friendly form of a SlotChoice (stored in jobs.plan_json)."""
    return {
        "start": choice.start.isoformat(),
        "forecast_mean": choice.forecast_mean,
        "forecast_unc": choice.forecast_unc,
        "forecast_now_mean": choice.forecast_now_mean,
        "expected_saving": choice.expected_saving,
        "deferred": choice.deferred,
        "reason": choice.reason,
        "candidates": [[s.isoformat(), m, u] for s, m, u in choice.candidates],
    }
