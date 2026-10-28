"""M1: override timeline, actual/forecast providers, mean_intensity, simulated clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greensettle.intensity.provider import IntensityReading, SimulatedCurve
from greensettle.intensity.timeline import (
    ActualProvider,
    ForecastErrorModel,
    ForecastProvider,
    OverrideTimeline,
    mean_intensity,
    sample_times,
)
from greensettle.server.clock import Clock

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
H = timedelta(hours=1)


def curve() -> SimulatedCurve:
    return SimulatedCurve(tz=UTC)  # explicit tz: results do not depend on the test machine


# ---------------------------------------------------------------- override timeline
def test_timeline_defaults_to_off_and_picks_latest_override():
    tl = OverrideTimeline()
    assert tl.state_at(T0) == "off"
    tl.add(T0, "green")
    tl.add(T0 + 2 * H, "dirty")
    tl.add(T0 + 4 * H, "off")
    assert tl.state_at(T0 - timedelta(seconds=1)) == "off"
    assert tl.state_at(T0) == "green"  # from_sim <= t: the override applies AT its start
    assert tl.state_at(T0 + H) == "green"
    assert tl.state_at(T0 + 2 * H) == "dirty"
    assert tl.state_at(T0 + 5 * H) == "off"


def test_a_flip_does_not_change_the_past():
    actual = ActualProvider(curve())
    past = [actual.get(T0 + i * timedelta(minutes=10)) for i in range(6)]
    actual.timeline.add(T0 + H, "dirty")  # flip at "now" = T0 + 1 h
    assert [actual.get(T0 + i * timedelta(minutes=10)) for i in range(6)] == past
    assert actual.get(T0 + H).g_per_kwh == SimulatedCurve.DIRTY_G_PER_KWH
    assert actual.get(T0 + 3 * H).g_per_kwh == SimulatedCurve.DIRTY_G_PER_KWH


def test_timeline_rejects_rewriting_history_and_bad_input():
    tl = OverrideTimeline()
    tl.add(T0 + H, "green")
    tl.add(T0 + H, "dirty")  # same moment is allowed (latest one wins)
    assert tl.state_at(T0 + H) == "dirty"
    with pytest.raises(ValueError, match="no rewriting the past"):
        tl.add(T0, "green")
    with pytest.raises(ValueError):
        tl.add(T0 + 2 * H, "purple")
    with pytest.raises(ValueError):
        tl.add(datetime(2026, 11, 2, 9), "green")  # naive


def test_actual_provider_values():
    c = curve()
    actual = ActualProvider(c)
    reading = actual.get(T0)
    assert reading.g_per_kwh == pytest.approx(c.curve_value(T0))
    assert reading.unc_g_per_kwh == pytest.approx(0.10 * reading.g_per_kwh)
    actual.timeline.add(T0, "green")
    assert actual.get(T0) == IntensityReading(80.0, 8.0, "actual:override-green")


# ---------------------------------------------------------------- forecast
def test_forecast_is_deterministic():
    a, b = ForecastProvider(curve()), ForecastProvider(curve())
    targets = [T0 + i * timedelta(minutes=17) for i in range(50)]
    assert [a.point(t, T0) for t in targets] == [b.point(t, T0) for t in targets]
    assert a.mean(T0 + H, T0 + 2 * H, T0) == b.mean(T0 + H, T0 + 2 * H, T0)


def test_forecast_seed_changes_the_noise():
    a = ForecastProvider(curve(), ForecastErrorModel(seed=1))
    b = ForecastProvider(curve(), ForecastErrorModel(seed=2))
    assert a.point(T0 + 3 * H, T0) != b.point(T0 + 3 * H, T0)


def test_forecast_error_grows_with_lead_time_and_is_capped():
    m = ForecastErrorModel()
    assert m.relative_sd(0) == pytest.approx(0.05)
    assert m.relative_sd(3 * 3600) == pytest.approx(0.11)
    assert m.relative_sd(100 * 3600) == pytest.approx(0.40)  # capped
    assert m.relative_sd(-3600) == pytest.approx(0.05)  # a target in the past counts as lead 0

    c = curve()
    f = ForecastProvider(c)
    target = T0 + 10 * H
    errors, uncs = [], []
    for lead_h in (0, 2, 5, 10):
        value, unc = f.point(target, issued_at=target - lead_h * H)
        errors.append(abs(value / c.curve_value(target) - 1))
        uncs.append(unc / value)
    # Same target hour -> same z, so the error scales exactly with the sd.
    assert errors == sorted(errors) and errors[-1] > errors[0]
    assert uncs == pytest.approx([0.05, 0.09, 0.15, 0.25])


def test_forecast_bias_and_no_overrides():
    c = curve()
    exact = ForecastProvider(c, ForecastErrorModel(bias=0.1, base_sd=0.0, sd_per_hour=0.0))
    value, unc = exact.point(T0 + 4 * H, T0)
    assert value == pytest.approx(1.1 * c.curve_value(T0 + 4 * H))
    assert unc == 0.0
    # The forecast never sees demo overrides; only ActualProvider does.
    actual = ActualProvider(c)
    actual.timeline.add(T0, "dirty")
    assert actual.get(T0 + 4 * H).g_per_kwh == 700.0
    assert exact.point(T0 + 4 * H, T0)[0] != pytest.approx(700.0)


def test_forecast_bound_matches_mean():
    f = ForecastProvider(curve())
    bound = f.bound(issued_at=T0)
    assert bound(T0 + 2 * H, T0 + 2 * H + timedelta(minutes=10)) == f.mean(
        T0 + 2 * H, T0 + 2 * H + timedelta(minutes=10), T0
    )


# ---------------------------------------------------------------- sampling and mean_intensity
def test_sample_times():
    assert sample_times(T0, T0 + timedelta(seconds=20)) == [T0 + timedelta(seconds=10)]  # midpoint
    ten_min = sample_times(T0, T0 + timedelta(minutes=10))
    assert len(ten_min) == 11 and ten_min[0] == T0 and ten_min[-1] == T0 + timedelta(minutes=10)
    assert ten_min[1] - ten_min[0] == timedelta(minutes=1)
    five_h = sample_times(T0, T0 + 5 * H)
    assert len(five_h) == 60 and five_h[-1] == T0 + 5 * H
    with pytest.raises(ValueError):
        sample_times(T0, T0 - H)


class Constant:
    def __init__(self, g: float, rel: float = 0.1) -> None:
        self.g, self.rel = g, rel

    def get(self, t: datetime) -> IntensityReading:
        return IntensityReading(self.g, self.g * self.rel, "const")


def test_mean_intensity_constant_and_uncertainty():
    mean, unc = mean_intensity(Constant(300.0), T0, T0 + H)
    assert mean == pytest.approx(300.0) and unc == pytest.approx(30.0)


def test_mean_intensity_across_an_override():
    actual = ActualProvider(curve())
    actual.timeline.add(T0, "green")
    actual.timeline.add(T0 + timedelta(minutes=20), "dirty")
    mean, unc = mean_intensity(actual, T0, T0 + timedelta(minutes=40))
    # 41 one-minute samples (minutes 0..40): 20 at 80 g/kWh, 21 at 700 g/kWh
    assert mean == pytest.approx((20 * 80 + 21 * 700) / 41)
    assert unc == pytest.approx(0.1 * mean)


# ---------------------------------------------------------------- clock
class FakeRealTime:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_clock_scale():
    rt = FakeRealTime()
    clock = Clock.start(sim_start=T0, scale=600, real_time=rt)
    assert clock.now() == T0
    rt.t += 6  # 6 real seconds at x600 = 1 sim hour
    assert clock.now() == T0 + H
    assert clock.sim_seconds(25) == 15_000
    assert clock.real_to_sim(rt.t - 6) == T0


def test_clock_set_scale_does_not_jump():
    rt = FakeRealTime()
    clock = Clock.start(sim_start=T0, scale=600, real_time=rt)
    rt.t += 6
    clock.set_scale(60)
    assert clock.now() == T0 + H  # no jump at the moment of change
    rt.t += 60  # 60 real s at x60 = 1 sim hour
    assert clock.now() == T0 + 2 * H


def test_clock_resume_after_restart_keeps_continuity():
    rt = FakeRealTime()
    before = Clock.start(sim_start=T0, scale=600, real_time=rt)
    saved = (before.anchor_sim, before.anchor_real, before.scale)
    rt.t += 12  # server down for 12 real s (old scale: 2 sim hours pass)
    after = Clock.resume(*saved, new_scale=1, real_time=rt)
    assert after.now() == T0 + 2 * H
    assert after.anchor_real == rt.t and after.scale == 1
    rt.t += 3600
    assert after.now() == T0 + 3 * H


def test_clock_validation_and_utc():
    with pytest.raises(ValueError):
        Clock(datetime(2026, 1, 1), 0.0)
    with pytest.raises(ValueError):
        Clock.start(sim_start=T0, scale=0)
    ist = timezone(timedelta(hours=5, minutes=30))
    clock = Clock(datetime(2026, 1, 1, 10, tzinfo=ist), 0.0, real_time=lambda: 0.0)
    assert clock.now().tzinfo == UTC and clock.now().hour == 4
