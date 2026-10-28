"""Tests for choose_slot (greensettle/scheduler/slots.py).

These are EXPECTED TO FAIL with NotImplementedError until choose_slot is
written. Nothing is skipped or xfail on purpose.

The fake forecast below is defined per candidate INDEX i, where the
candidate start is t0 + i * 1800 s. It also records every call, so the tests
can check that only candidates inside the wait window were asked about.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from greensettle.scheduler.slots import (
    REASONS,
    SchedulerParams,
    SlotChoice,
    SlotRequest,
    choose_slot,
)

T0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
SLOT = 1800.0
D = 600.0  # est_duration_s in SIM seconds


class FakeForecast:
    def __init__(self, per_index: dict[int, tuple[float, float]], default: tuple[float, float]) -> None:
        self.per_index = per_index
        self.default = default
        self.calls: list[tuple[datetime, datetime]] = []

    def __call__(self, start: datetime, end: datetime) -> tuple[float, float]:
        self.calls.append((start, end))
        offset = (start - T0).total_seconds()
        index = round(offset / SLOT)
        assert offset == index * SLOT, "candidate starts must be t0 + i * slot_seconds"
        return self.per_index.get(index, self.default)

    def called_indices(self) -> list[int]:
        return [round((s - T0).total_seconds() / SLOT) for s, _ in self.calls]


def forecast(spec: dict, default: tuple[float, float]) -> FakeForecast:
    """spec maps an index, or a range of indices, to (mean, unc)."""
    per_index: dict[int, tuple[float, float]] = {}
    for key, value in spec.items():
        for i in (key if isinstance(key, range) else [key]):
            per_index[i] = value
    return FakeForecast(per_index, default)


def request(max_delay_h: float, d: float = D) -> SlotRequest:
    return SlotRequest(submit_time=T0, max_delay_h=max_delay_h, est_duration_s=d)


def at(i: int) -> datetime:
    return T0 + timedelta(seconds=i * SLOT)


# Forecasts for the vectors -------------------------------------------------
def v1() -> FakeForecast:
    return forecast({0: (600, 60), range(1, 4): (580, 58), range(4, 6): (150, 15), range(6, 9): (400, 40)},
                    default=(999, 99))


def v2() -> FakeForecast:
    return forecast({0: (400, 40), range(1, 3): (395, 40), 3: (380, 38)}, default=(400, 40))


def assert_starts_now(choice: SlotChoice, reason: str, f0: float, u0: float) -> None:
    assert choice.reason == reason
    assert choice.deferred is False
    assert choice.start == T0
    assert choice.forecast_mean == pytest.approx(f0)
    assert choice.forecast_unc == pytest.approx(u0)
    assert choice.forecast_now_mean == pytest.approx(f0)
    assert choice.expected_saving == pytest.approx(0.0)


# ---------------------------------------------------------------- the eight vectors
def test_v1_deferred_to_the_green_dip():
    f = v1()
    c = choose_slot(request(4), f, SchedulerParams())
    assert c.reason == "deferred" and c.deferred is True
    assert c.start == T0 + timedelta(seconds=7200)  # i4; i5 ties but is later
    assert c.forecast_mean == pytest.approx(150) and c.forecast_unc == pytest.approx(15)
    assert c.forecast_now_mean == pytest.approx(600)
    assert c.expected_saving == pytest.approx(450)


def test_v2_gain_within_noise():
    # saving 400 - 380 = 20; unc = hypot(40, 38) = 55.17; 20 <= 1 * 55.17
    c = choose_slot(request(4), v2(), SchedulerParams())
    assert_starts_now(c, "gain_within_noise", 400, 40)
    assert math.hypot(40, 38) == pytest.approx(55.17, abs=0.01)


def test_v3_same_as_v2_with_z_zero_defers():
    c = choose_slot(request(4), v2(), SchedulerParams(defer_z=0.0))
    assert c.reason == "deferred" and c.deferred
    assert c.start == T0 + timedelta(seconds=5400)  # i3
    assert c.expected_saving == pytest.approx(20)
    assert c.forecast_mean == pytest.approx(380) and c.forecast_unc == pytest.approx(38)


def test_v4_no_window():
    f = forecast({}, default=(123, 12))
    c = choose_slot(request(0), f, SchedulerParams())
    assert_starts_now(c, "no_window", 123, 12)
    assert len(c.candidates) == 1
    assert f.called_indices() == [0]


def test_v5_tie_goes_to_earliest():
    f = forecast({0: (500, 50), 2: (100, 10), 5: (100, 10)}, default=(500, 50))
    c = choose_slot(request(4), f, SchedulerParams())
    assert c.reason == "deferred"
    assert c.start == at(2) == T0 + timedelta(seconds=3600)


def test_v6_candidate_outside_the_window_is_ignored():
    f = forecast({9: (50, 5)}, default=(500, 50))  # i9 = t0 + 4.5 h, outside a 4 h window
    c = choose_slot(request(4), f, SchedulerParams())
    assert_starts_now(c, "already_best", 500, 50)  # all ties -> earliest -> i0
    assert len(c.candidates) == 9  # N = 8 -> i0..i8
    assert sorted(f.called_indices()) == list(range(9))


def test_v7_window_not_a_multiple_of_the_slot():
    f = forecast({8: (100, 10)}, default=(500, 50))  # 3.9 h -> N = floor(7.8) = 7
    c = choose_slot(request(3.9), f, SchedulerParams())
    assert_starts_now(c, "already_best", 500, 50)
    assert len(c.candidates) == 8
    assert 8 not in f.called_indices()


def test_v8_same_as_v1_with_huge_z():
    # saving 450 <= 100 * hypot(60, 15) = 6185
    c = choose_slot(request(4), v1(), SchedulerParams(defer_z=100.0))
    assert_starts_now(c, "gain_within_noise", 600, 60)


# ---------------------------------------------------------------- candidates and calls
def test_candidates_are_filled_in_order():
    f = v1()
    c = choose_slot(request(4), f, SchedulerParams())
    assert isinstance(c.candidates, tuple)
    assert [cand[0] for cand in c.candidates] == [at(i) for i in range(9)]
    expected = [(600, 60)] + [(580, 58)] * 3 + [(150, 15)] * 2 + [(400, 40)] * 3
    for cand, (mean, unc) in zip(c.candidates, expected):
        assert cand[1] == pytest.approx(mean) and cand[2] == pytest.approx(unc)


def test_forecast_called_once_per_candidate_with_the_job_duration():
    f = v1()
    choose_slot(request(4, d=750.0), f, SchedulerParams())
    assert sorted(f.called_indices()) == list(range(9))
    assert all(end - start == timedelta(seconds=750) for start, end in f.calls)


def test_slot_seconds_parameter_is_used():
    # 1 h window with 900 s slots -> N = 4 -> starts every 15 minutes
    calls: list[datetime] = []

    def flat(start: datetime, end: datetime) -> tuple[float, float]:
        calls.append(start)
        return 300.0, 30.0

    c = choose_slot(request(1), flat, SchedulerParams(slot_seconds=900))
    assert [x[0] for x in c.candidates] == [T0 + timedelta(seconds=900 * i) for i in range(5)]
    assert sorted(calls) == [T0 + timedelta(seconds=900 * i) for i in range(5)]


def test_reason_is_always_one_of_the_four():
    for maker, z in ((v1, 1.0), (v2, 1.0), (v2, 0.0), (v1, 100.0)):
        assert choose_slot(request(4), maker(), SchedulerParams(defer_z=z)).reason in REASONS


def test_deferral_never_moves_to_a_worse_slot():
    # Even with z = 0, a deferral must be to a strictly lower forecast.
    f = forecast({0: (300, 30)}, default=(300, 30))  # flat: nothing to gain
    c = choose_slot(request(4), f, SchedulerParams(defer_z=0.0))
    assert_starts_now(c, "already_best", 300, 30)


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "req, params",
    [
        (SlotRequest(T0, -0.5, D), SchedulerParams()),
        (SlotRequest(T0, 4, 0.0), SchedulerParams()),
        (SlotRequest(T0, 4, -10.0), SchedulerParams()),
        (SlotRequest(T0, 4, D), SchedulerParams(slot_seconds=0.0)),
        (SlotRequest(T0, 4, D), SchedulerParams(slot_seconds=-1800.0)),
        (SlotRequest(T0, 4, D), SchedulerParams(defer_z=-0.1)),
    ],
    ids=["neg-delay", "zero-duration", "neg-duration", "zero-slot", "neg-slot", "neg-z"],
)
def test_validation_raises_value_error(req, params):
    f = v1()
    with pytest.raises(ValueError):
        choose_slot(req, f, params)
    assert f.calls == []  # validated before asking the forecast anything
