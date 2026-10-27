"""Integration math, sampler and idle calibration."""

from __future__ import annotations

import logging

import pytest

from greensettle.agent import meter
from greensettle.agent.lhm_client import LhmClient
from greensettle.agent.meter import Sample


def samples_from(fn, t_end: float, step: float) -> list[Sample]:
    n = round(t_end / step)
    return [Sample(t=i * step, watts=fn(i * step)) for i in range(n + 1)]


def test_constant_power_gives_p_times_t():
    samples = samples_from(lambda t: 20.0, t_end=30, step=1.0)
    assert meter.integrate_trapezoid(samples) == pytest.approx(20.0 * 30)
    assert meter.integrate_left_riemann(samples) == pytest.approx(20.0 * 30)


def test_constant_power_with_uneven_spacing():
    samples = [Sample(0.0, 10.0), Sample(0.7, 10.0), Sample(2.1, 10.0), Sample(3.0, 10.0)]
    assert meter.integrate_trapezoid(samples) == pytest.approx(30.0)


def test_linear_ramp_known_area():
    # P(t) = 5 + 2t from t=0..10  ->  area = 5*10 + 10^2 = 150 J (trapezoid is exact for lines)
    samples = samples_from(lambda t: 5 + 2 * t, t_end=10, step=1.0)
    assert meter.integrate_trapezoid(samples) == pytest.approx(150.0)
    # Left Riemann under-estimates a rising ramp by exactly slope*step*T/2 = 2*1*10/2 = 10 J
    assert meter.integrate_left_riemann(samples) == pytest.approx(140.0)


def test_too_few_samples_give_zero():
    assert meter.integrate_trapezoid([]) == 0.0
    assert meter.integrate_trapezoid([Sample(0, 5)]) == 0.0


def test_joules_to_kwh():
    assert meter.joules_to_kwh(3_600_000) == pytest.approx(1.0)
    assert meter.joules_to_kwh(36.0) == pytest.approx(1e-5)


def test_sampler_stats_from_known_samples():
    s = [Sample(0.0, 5), Sample(1.0, 5), Sample(2.2, 6), Sample(3.0, 6)]
    stats = meter.sampler_stats(s)
    assert stats.samples == 4
    assert stats.mean_interval_s == pytest.approx(1.0)
    assert stats.jitter_s > 0
    assert stats.unchanged_fraction == pytest.approx(2 / 3)


def test_sampler_end_to_end_with_mock_server(mock_lhm):
    import time

    client = LhmClient(mock_lhm.url)
    sampler = meter.Sampler(client.package_watts, interval_s=0.05)
    sampler.start()
    time.sleep(0.5)
    sampler.stop()
    samples = sampler.samples
    assert len(samples) >= 5
    assert all(b.t > a.t for a, b in zip(samples, samples[1:]))  # monotonic timestamps
    stats = sampler.stats()
    assert stats.mean_interval_s == pytest.approx(0.05, abs=0.03)
    assert stats.unchanged_fraction == 1.0  # the mock never changes value
    assert mock_lhm.requests == len(samples)


def test_sampler_survives_failed_reads():
    calls = {"n": 0}

    def flaky() -> float:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("simulated network hiccup")
        return 10.0

    import time

    sampler = meter.Sampler(flaky, interval_s=0.02)
    sampler.start()
    time.sleep(0.2)
    sampler.stop()
    assert sampler.failed_reads > 0
    assert len(sampler.samples) > 0


def test_calibrate_idle_mean_std_and_file(tmp_path):
    values = iter([4.0, 5.0] * 100)
    path = tmp_path / "idle.json"
    idle = meter.calibrate_idle(lambda: next(values), seconds=0.3,
                                config=meter.MeterConfig(poll_interval_s=0.02), path=path)
    assert idle.mean_w == pytest.approx(4.5, abs=0.3)
    assert idle.std_w > 0.3
    loaded = meter.IdleCalibration.load(path)
    assert loaded == idle
    assert "T" in loaded.measured_at  # ISO timestamp


def test_calibrate_idle_warns_when_not_idle(tmp_path, caplog):
    values = iter([2.0, 30.0] * 100)
    with caplog.at_level(logging.WARNING):
        meter.calibrate_idle(lambda: next(values), seconds=0.2,
                             config=meter.MeterConfig(poll_interval_s=0.02), path=tmp_path / "i.json")
    assert "may not be idle" in caplog.text


def test_missing_idle_file_has_clear_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="calibrate-idle"):
        meter.IdleCalibration.load(tmp_path / "nope.json")
