"""Carbon intensity for the Phase 3 server: ACTUAL vs FORECAST.

The server needs two different views of the same simulated grid:

  ActualProvider    "what really happened". The SimulatedCurve plus a
                    timeline of demo overrides (green / dirty / off). Used
                    for settlement: i_run and the counterfactual i_submit.

  ForecastProvider  "what we believed in advance". The same curve WITHOUT
                    overrides, plus a deterministic forecast error that grows
                    with lead time. Used only by the scheduler to plan.

Because the scheduler plans on the forecast but jobs are settled on actual
data, a demo override (e.g. flip to "dirty") can make a planned deferral
backfire, which is exactly what settlement must be able to detect.

All datetimes here are SIMULATED time, timezone-aware, UTC.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol

from greensettle.intensity.provider import IntensityReading, SimulatedCurve

OVERRIDE_STATES = ("green", "dirty", "off")

# A forecast already bound to an issue time: (start, end) -> (mean g/kWh, uncertainty g/kWh).
# This is the shape choose_slot() expects.
ForecastMeanFn = Callable[[datetime, datetime], tuple[float, float]]


class PointProvider(Protocol):
    def get(self, timestamp: datetime) -> IntensityReading: ...


# --------------------------------------------------------------------------
# Override timeline
# --------------------------------------------------------------------------
@dataclass
class OverrideTimeline:
    """An append-only list of (from_sim, state), sorted by time.

    state_at(t) is the state of the LATEST override with from_sim <= t,
    or "off" if there is none.

    Why a timeline instead of a single current setting: if a flip to "dirty"
    rewrote the past as well, the counterfactual ("what if the job had
    started at submission?") would change with it, and a deferral could never
    be shown to backfire. So a new override only affects times from
    `from_sim` onwards, and it may not be placed before an existing one.
    """

    entries: list[tuple[datetime, str]] = field(default_factory=list)

    def add(self, from_sim: datetime, state: str) -> None:
        if state not in OVERRIDE_STATES:
            raise ValueError(f"state must be one of {OVERRIDE_STATES}, not {state!r}")
        if from_sim.tzinfo is None:
            raise ValueError("from_sim must be timezone-aware")
        if self.entries and from_sim < self.entries[-1][0]:
            raise ValueError("overrides may only be appended at or after the latest one (no rewriting the past)")
        self.entries.append((from_sim, state))

    def state_at(self, t: datetime) -> str:
        starts = [start for start, _ in self.entries]
        i = bisect.bisect_right(starts, t)  # number of overrides with from_sim <= t
        return self.entries[i - 1][1] if i > 0 else "off"


class ActualProvider:
    """The 'real' simulated grid: the curve, unless an override says otherwise."""

    def __init__(self, curve: SimulatedCurve, timeline: OverrideTimeline | None = None) -> None:
        self.curve = curve
        self.timeline = timeline if timeline is not None else OverrideTimeline()

    def get(self, timestamp: datetime) -> IntensityReading:
        state = self.timeline.state_at(timestamp)
        if state == "green":
            g, source = SimulatedCurve.GREEN_G_PER_KWH, "actual:override-green"
        elif state == "dirty":
            g, source = SimulatedCurve.DIRTY_G_PER_KWH, "actual:override-dirty"
        else:
            g, source = self.curve.curve_value(timestamp), f"actual:curve(seed={self.curve.seed})"
        return IntensityReading(g, g * self.curve.relative_unc, source)


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ForecastErrorModel:
    """How wrong the forecast is. ALL VALUES ARE PLACEHOLDERS, not measured.

    relative sd(lead) = min(max_sd, base_sd + sd_per_hour * lead_hours)
    """

    bias: float = 0.0  # PLACEHOLDER: systematic over/under-forecast (0.05 = 5 % too high)
    base_sd: float = 0.05  # PLACEHOLDER: error even for "right now"
    sd_per_hour: float = 0.02  # PLACEHOLDER: extra error per hour of lead time
    max_sd: float = 0.40  # PLACEHOLDER: the error stops growing here
    seed: int = 1234

    def relative_sd(self, lead_s: float) -> float:
        lead_h = max(0.0, lead_s) / 3600
        return min(self.max_sd, self.base_sd + self.sd_per_hour * lead_h)


class ForecastProvider:
    """forecast(target, issued_at) = curve(target) * (1 + bias + noise).

    noise = relative_sd(lead) * z, where z is a standard normal number drawn
    from random.Random seeded by (seed, target hour). So:
      - the same question always gets the same answer (deterministic demos),
      - forecasts for the same hour share the same z, and the error is just
        scaled up the further ahead the forecast is issued.
    Overrides are deliberately NOT visible to the forecast.
    """

    def __init__(self, curve: SimulatedCurve, errors: ForecastErrorModel = ForecastErrorModel()) -> None:
        self.curve = curve
        self.errors = errors

    def _z(self, target: datetime) -> float:
        hour_index = math.floor(target.timestamp() / 3600)
        return random.Random(f"{self.errors.seed}:{hour_index}").gauss(0.0, 1.0)

    def point(self, target: datetime, issued_at: datetime) -> tuple[float, float]:
        """(forecast g/kWh, uncertainty g/kWh) for one moment."""
        sd = self.errors.relative_sd((target - issued_at).total_seconds())
        factor = max(0.0, 1.0 + self.errors.bias + sd * self._z(target))  # intensity cannot be negative
        value = self.curve.curve_value(target) * factor
        return value, sd * value

    def mean(self, start: datetime, end: datetime, issued_at: datetime) -> tuple[float, float]:
        """Average forecast over [start, end] and its uncertainty.

        The uncertainty is the AVERAGE of the point uncertainties, i.e. we
        assume the errors at nearby times are fully correlated (they are: one
        weather system). That is the cautious choice; averaging independent
        errors would shrink it.
        """
        points = [self.point(t, issued_at) for t in sample_times(start, end)]
        return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))

    def bound(self, issued_at: datetime) -> ForecastMeanFn:
        """A forecast frozen at one issue time, in the form choose_slot() expects."""

        def forecast_mean(start: datetime, end: datetime) -> tuple[float, float]:
            return self.mean(start, end, issued_at)

        return forecast_mean


# --------------------------------------------------------------------------
# Averaging over an interval
# --------------------------------------------------------------------------
MAX_POINTS = 60
STEP_S = 60.0  # one simulated minute


def sample_times(start: datetime, end: datetime, max_points: int = MAX_POINTS) -> list[datetime]:
    """Evenly spaced moments in [start, end]: about one per sim minute, at most 60.

    Intervals shorter than a minute are represented by their midpoint.
    """
    if end < start:
        raise ValueError("end must not be before start")
    duration = (end - start).total_seconds()
    n = min(max_points, int(duration // STEP_S) + 1)
    if n <= 1:
        return [start + timedelta(seconds=duration / 2)]
    step = duration / (n - 1)
    return [start + timedelta(seconds=i * step) for i in range(n)]


def mean_intensity(provider: PointProvider, start: datetime, end: datetime) -> tuple[float, float]:
    """Average intensity over [start, end] and its uncertainty.

    SIMPLIFICATION (see docs): one mean per job; a job's energy is assumed to
    be spread evenly over its run. The uncertainty is the provider's own
    relative uncertainty applied to the mean (a PLACEHOLDER).
    """
    readings = [provider.get(t) for t in sample_times(start, end)]
    n = len(readings)
    return (sum(r.g_per_kwh for r in readings) / n, sum(r.unc_g_per_kwh for r in readings) / n)
