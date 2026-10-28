"""Tests for compute_settlement (greensettle/settlement/rules.py).

Three groups:
  1. the seven verified test vectors (A-G), rel tolerance 1e-4,
  2. property tests over random valid inputs (seeded, so every run checks
     exactly the same cases),
  3. input / parameter validation.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

import pytest

from greensettle.settlement.rules import Settlement, SettlementParams, compute_settlement

REL = 1e-4
E, DE = 0.000175, 0.0000169  # from the Phase 1 worked example
N_RANDOM = 2500


def settle(i_submit, i_submit_unc, i_run, i_run_unc, params=SettlementParams()) -> Settlement:
    return compute_settlement(E, DE, i_submit, i_submit_unc, i_run, i_run_unc, params)


def slack(x: float) -> float:
    """Allowed float rounding noise around a value of size x."""
    return 1e-9 * max(1.0, abs(x))


# ---------------------------------------------------------------- 1. vectors
# (name, (i_submit, unc, i_run, unc), params, zone, fee, rebate, surcharge, net)
VECTORS = [
    ("A", (700, 70, 80, 8), SettlementParams(), "saving", 14.0, 8.4, 0.0, 5.6),
    ("B", (100, 10, 80, 8), SettlementParams(), "saving", 14.0, 0.6168, 0.0, 13.3832),
    ("C", (82, 8.2, 80, 8), SettlementParams(), "dead_zone", 14.0, 0.0, 0.0, 14.0),
    ("D", (80, 8, 700, 70), SettlementParams(), "backfire", 122.5, 0.0, 0.0, 122.5),
    ("E", (80, 8, 700, 70), SettlementParams(sigma=0.5), "backfire", 122.5, 0.0, 46.1597, 168.6597),
    ("F", (82, 8.2, 80, 8), SettlementParams(k=0.0), "saving", 14.0, 0.175, 0.0, 13.825),
    ("G", (100, 10, 80, 8), SettlementParams(k=2.0), "dead_zone", 14.0, 0.0, 0.0, 14.0),
]


@pytest.mark.parametrize("name, intens, params, zone, fee, rebate, surcharge, net", VECTORS,
                         ids=[v[0] for v in VECTORS])
def test_vectors(name, intens, params, zone, fee, rebate, surcharge, net):
    s = settle(*intens, params)
    assert isinstance(s, Settlement)
    assert s.zone == zone
    assert s.fee == pytest.approx(fee, rel=REL)
    assert s.rebate == pytest.approx(rebate, rel=REL)
    assert s.surcharge == pytest.approx(surcharge, rel=REL)
    assert s.net == pytest.approx(net, rel=REL)


def test_vector_a_saved_and_uncertainty():
    s = settle(700, 70, 80, 8)
    assert s.saved_g == pytest.approx(0.1085, rel=REL)
    assert s.saved_unc_g == pytest.approx(0.01618, rel=REL)


# ---------------------------------------------------------------- 2. properties
def random_case(rng: random.Random) -> tuple[tuple[float, ...], SettlementParams]:
    e = rng.choice([0.0, rng.uniform(0, 0.01), rng.uniform(0, 1e-4)])
    i_submit, i_run = rng.uniform(0, 900), rng.uniform(0, 900)
    inputs = (
        e,
        rng.choice([0.0, rng.uniform(0, 0.5) * e]),
        i_submit,
        rng.choice([0.0, rng.uniform(0, 0.3) * i_submit]),
        i_run,
        rng.choice([0.0, rng.uniform(0, 0.3) * i_run]),
    )
    params = SettlementParams(
        p=rng.choice([1000.0, rng.uniform(1, 5000)]),
        rho=rng.choice([0.5, rng.uniform(0, 1)]),
        gamma=rng.choice([0.0, 0.6, rng.uniform(0, 0.99)]),
        k=rng.choice([0.0, 1.0, rng.uniform(0, 3)]),
        sigma=rng.choice([0.0, rng.uniform(0, 2)]),
    )
    return inputs, params


def random_cases(seed: int, n: int = N_RANDOM):
    rng = random.Random(seed)
    for _ in range(n):
        yield random_case(rng)


def test_property_no_credit_farming():
    """rebate <= gamma * fee, and net > 0 whenever fee > 0, in every zone."""
    zones = set()
    for inputs, params in random_cases(1):
        s = compute_settlement(*inputs, params)
        zones.add(s.zone)
        assert s.rebate <= params.gamma * s.fee + slack(s.fee), (inputs, params, s)
        if s.fee > 0:
            assert s.net > 0, (inputs, params, s)
    assert zones == {"saving", "dead_zone", "backfire"}  # every zone was exercised


def test_property_rebate_and_surcharge_only_in_their_zone():
    for inputs, params in random_cases(2):
        s = compute_settlement(*inputs, params)
        if s.zone != "saving":
            assert s.rebate == 0, (inputs, params, s)
        if s.zone != "backfire" or params.sigma == 0:
            assert s.surcharge == 0, (inputs, params, s)
        assert s.net == pytest.approx(s.fee + s.surcharge - s.rebate, rel=1e-12, abs=1e-12)


def test_property_raising_k_never_creates_a_saving():
    rng = random.Random(3)
    for inputs, params in random_cases(3):
        before = compute_settlement(*inputs, params)
        after = compute_settlement(*inputs, replace(params, k=params.k + rng.uniform(0, 3)))
        if before.zone in ("dead_zone", "backfire"):
            assert after.zone != "saving", (inputs, params, before, after)
        assert after.rebate <= before.rebate + slack(before.rebate)


def test_property_more_uncertainty_never_increases_the_rebate():
    rng = random.Random(4)
    for inputs, params in random_cases(4):
        e, de, i_s, ui_s, i_r, ui_r = inputs
        factor = 1 + rng.uniform(0, 5)
        wider = (e, de * factor + rng.uniform(0, 1e-5), i_s, ui_s * factor + rng.uniform(0, 20),
                 i_r, ui_r * factor + rng.uniform(0, 20))
        before = compute_settlement(*inputs, params)
        after = compute_settlement(*wider, params)
        assert after.rebate <= before.rebate + slack(before.rebate), (inputs, wider, params)


def test_k_zero_gives_naive_ex_post():
    # With k = 0 any positive measured saving earns a rebate (vector F is one case).
    s = settle(80.5, 8, 80, 8, SettlementParams(k=0.0))
    assert s.zone == "saving" and s.rebate > 0


# ---------------------------------------------------------------- 3. validation
@pytest.mark.parametrize("gamma", [1.0, 1.5, math.inf, math.nan])
def test_gamma_must_be_below_one(gamma):
    with pytest.raises(ValueError):
        settle(700, 70, 80, 8, SettlementParams(gamma=gamma))


@pytest.mark.parametrize("change", [{"p": 0.0}, {"p": -1.0}, {"rho": -0.1}, {"k": -1.0}, {"sigma": -0.5},
                                    {"gamma": -0.1}])
def test_negative_or_zero_params_rejected(change):
    with pytest.raises(ValueError):
        settle(700, 70, 80, 8, replace(SettlementParams(), **change))


@pytest.mark.parametrize("position", range(6))
@pytest.mark.parametrize("bad", [-1e-9, math.nan, math.inf, -math.inf])
def test_bad_inputs_rejected(position, bad):
    inputs = [E, DE, 700, 70, 80, 8]
    inputs[position] = bad
    with pytest.raises(ValueError):
        compute_settlement(*inputs)


def test_boundary_values_are_valid():
    s = compute_settlement(0, 0, 0, 0, 0, 0)
    assert (s.fee, s.rebate, s.surcharge, s.net, s.zone) == (0, 0, 0, 0, "dead_zone")
    s = settle(700, 70, 80, 8, SettlementParams(gamma=0.0, k=0.0, sigma=0.0, rho=0.0))
    assert s.rebate == 0 and s.net == s.fee


@pytest.mark.parametrize("field", ["p", "rho", "gamma", "k", "sigma"])
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_params_rejected(field, bad):
    with pytest.raises(ValueError):
        settle(700, 70, 80, 8, replace(SettlementParams(), **{field: bad}))
