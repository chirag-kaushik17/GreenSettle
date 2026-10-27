"""Simulated intensity curve, demo override, and gCO2e conversion."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from greensettle.engine.convert import to_gco2e
from greensettle.intensity import provider as ip

T0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
HOURS = [T0 + timedelta(minutes=30 * i) for i in range(96)]  # two days


def test_curve_is_deterministic_for_a_seed():
    a, b = ip.SimulatedCurve(seed=7), ip.SimulatedCurve(seed=7)
    assert [a.get(t) for t in HOURS] == [b.get(t) for t in HOURS]


def test_different_seed_gives_different_curve():
    a, b = ip.SimulatedCurve(seed=1), ip.SimulatedCurve(seed=2)
    assert [a.get(t).g_per_kwh for t in HOURS] != [b.get(t).g_per_kwh for t in HOURS]


def test_curve_has_midday_solar_dip_and_stays_plausible():
    curve = ip.SimulatedCurve()
    day = datetime(2026, 6, 1)  # naive = local time, matching how the curve reads the hour
    midday = curve.get(day.replace(hour=13)).g_per_kwh
    evening = curve.get(day.replace(hour=20)).g_per_kwh
    assert midday < evening
    values = [curve.get(t).g_per_kwh for t in HOURS]
    assert min(values) >= curve.floor_g
    assert max(values) < 1000


def test_reading_unpacks_like_a_tuple():
    g, unc, source = ip.SimulatedCurve().get(T0)
    assert g > 0 and unc == pytest.approx(0.10 * g) and source.startswith("simulated")


def test_force_state():
    curve = ip.SimulatedCurve()
    curve.force_state("green")
    green = curve.get(T0)
    curve.force_state("dirty")
    dirty = curve.get(T0)
    assert green.g_per_kwh == ip.SimulatedCurve.GREEN_G_PER_KWH
    assert dirty.g_per_kwh == ip.SimulatedCurve.DIRTY_G_PER_KWH
    assert dirty.g_per_kwh > green.g_per_kwh
    assert "forced-dirty" in dirty.source
    curve.force_state(None)
    assert curve.get(T0) == ip.SimulatedCurve().get(T0)
    with pytest.raises(ValueError):
        curve.force_state("purple")


def test_forced_state_file_roundtrip(tmp_path):
    assert ip.load_forced_state(tmp_path) is None
    ip.save_forced_state(tmp_path, "dirty")
    assert ip.load_forced_state(tmp_path) == "dirty"
    assert ip.make_provider("simulated", tmp_path).get(T0).g_per_kwh == 700.0
    ip.save_forced_state(tmp_path, None)
    assert ip.load_forced_state(tmp_path) is None


def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / ip.STATE_FILENAME).write_text("{not json")
    assert ip.load_forced_state(tmp_path) is None


# ---------------------------------------------------------------- conversion
def test_conversion_value_and_relative_quadrature():
    e = to_gco2e(energy_kwh=0.002, energy_unc_kwh=0.0002, g_per_kwh=500, unc_g_per_kwh=50)
    assert e.value_g == pytest.approx(1.0)
    # 10 % and 10 % relative errors -> sqrt(0.1^2 + 0.1^2) = 14.1 %
    assert e.unc_g / e.value_g == pytest.approx(math.sqrt(0.1**2 + 0.1**2))


def test_conversion_with_exact_intensity_keeps_energy_relative_error():
    e = to_gco2e(0.01, 0.001, 300, 0)
    assert e.unc_g / e.value_g == pytest.approx(0.1)


def test_conversion_zero_energy_does_not_divide_by_zero():
    e = to_gco2e(0.0, 0.0001, 400, 40)
    assert e.value_g == 0.0
    assert e.unc_g == pytest.approx(400 * 0.0001)
