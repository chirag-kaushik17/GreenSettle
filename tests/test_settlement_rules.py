"""Tests for compute_settlement (greensettle/settlement/rules.py).

These tests are EXPECTED TO FAIL with NotImplementedError until
compute_settlement is written. Nothing here is skipped or xfail on purpose.

Three groups:
  1. the seven verified test vectors (A-G),
  2. property tests over thousands of random valid inputs (seeded, so every
     run checks exactly the same cases),
  3. input / parameter validation.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from greensettle.settlement.rules import (
    ZONES,
    SettlementInputs,
    SettlementParams,
    SettlementResult,
    compute_settlement,
)

REL = 1e-3  # relative tolerance for the vectors
TINY = 1e-12  # absolute slack for float comparisons in property tests
N_RANDOM = 2500


def slack(x: float) -> float:
    """Allowed float rounding noise around a value of size x.

    Fees reach ~45,000 credits in the random cases, where one rounding step
    is already ~7e-12, so a fixed absolute slack would be too strict.
    """
    return 1e-9 * max(1.0, abs(x))

E, DE = 0.000175, 0.0000169  # from the Phase 1 worked example


def inputs(i_submit: float, i_submit_unc: float, i_run: float, i_run_unc: float) -> SettlementInputs:
    return SettlementInputs(E, DE, i_submit, i_submit_unc, i_run, i_run_unc)


# ---------------------------------------------------------------- 1. vectors
# (name, inputs, params, zone, fee, rebate, surcharge, net)
VECTORS = [
    ("A big saving, cap binds", inputs(700, 70, 80, 8), SettlementParams(),
     "saving", 14.0, 8.4, 0.0, 5.6),
    ("B modest saving, rho binds", inputs(100, 10, 80, 8), SettlementParams(),
     "saving", 14.0, 0.6168, 0.0, 13.3832),
    ("C inside the noise", inputs(82, 8.2, 80, 8), SettlementParams(),
     "dead_zone", 14.0, 0.0, 0.0, 14.0),
    ("D backfire", inputs(80, 8, 700, 70), SettlementParams(),
     "backfire", 122.5, 0.0, 0.0, 122.5),
    ("E backfire, sigma=0.5", inputs(80, 8, 700, 70), SettlementParams(trap_surcharge_rate=0.5),
     "backfire", 122.5, 0.0, 46.1597, 168.6597),
    ("F case C with k=0", inputs(82, 8.2, 80, 8), SettlementParams(k_sigma=0.0),
     "saving", 14.0, 0.175, 0.0, 13.825),
    ("G case B with k=2", inputs(100, 10, 80, 8), SettlementParams(k_sigma=2.0),
     "dead_zone", 14.0, 0.0, 0.0, 14.0),
]


@pytest.mark.parametrize(
    "name, inp, params, zone, fee, rebate, surcharge, net", VECTORS, ids=[v[0][0] for v in VECTORS]
)
def test_vectors(name, inp, params, zone, fee, rebate, surcharge, net):
    r = compute_settlement(inp, params)
    assert isinstance(r, SettlementResult)
    assert r.zone == zone
    assert r.fee == pytest.approx(fee, rel=REL)
    assert r.rebate == pytest.approx(rebate, rel=REL, abs=1e-9)
    assert r.surcharge == pytest.approx(surcharge, rel=REL, abs=1e-9)
    assert r.net_cost == pytest.approx(net, rel=REL)


def test_vector_a_intermediate_values():
    r = compute_settlement(inputs(700, 70, 80, 8), SettlementParams())
    assert r.saved_g == pytest.approx(0.1085, rel=REL)
    assert r.saved_unc_g == pytest.approx(0.01618, rel=REL)
    assert r.saved_low_g == pytest.approx(0.09232, rel=REL)
    assert r.saved_high_g == pytest.approx(0.1085 + 0.01618, rel=REL)


def test_default_params_used_when_omitted():
    assert compute_settlement(inputs(700, 70, 80, 8)) == compute_settlement(inputs(700, 70, 80, 8), SettlementParams())


# ---------------------------------------------------------------- 2. properties
def random_inputs(rng: random.Random) -> SettlementInputs:
    e = rng.choice([0.0, rng.uniform(0, 0.01), rng.uniform(0, 1e-4)])
    i_submit = rng.uniform(0, 900)
    i_run = rng.uniform(0, 900)
    return SettlementInputs(
        energy_kwh=e,
        energy_unc_kwh=rng.choice([0.0, rng.uniform(0, 0.5) * e]),
        i_submit=i_submit,
        i_submit_unc=rng.choice([0.0, rng.uniform(0, 0.3) * i_submit]),
        i_run=i_run,
        i_run_unc=rng.choice([0.0, rng.uniform(0, 0.3) * i_run]),
    )


def random_params(rng: random.Random) -> SettlementParams:
    return SettlementParams(
        credits_per_gco2e=rng.choice([1000.0, rng.uniform(1, 5000)]),
        rebate_rate=rng.choice([1.0, rng.uniform(0.01, 1.0)]),
        rebate_cap=rng.choice([0.0, rng.uniform(0, 0.99)]),
        k_sigma=rng.choice([0.0, rng.uniform(0, 3)]),
        trap_surcharge_rate=rng.choice([0.0, rng.uniform(0, 2)]),
    )


def random_cases(seed: int, n: int = N_RANDOM):
    rng = random.Random(seed)
    for _ in range(n):
        yield random_inputs(rng), random_params(rng)


def test_property_rebate_capped_by_gamma_times_fee():
    for inp, params in random_cases(1):
        r = compute_settlement(inp, params)
        assert r.rebate <= params.rebate_cap * r.fee + slack(r.fee), (inp, params, r)


def test_property_no_rebate_without_proven_saving():
    for inp, params in random_cases(2):
        r = compute_settlement(inp, params)
        if r.saved_low_g <= 0:
            assert r.rebate == 0, (inp, params, r)
        bound = params.rebate_rate * params.credits_per_gco2e * max(r.saved_low_g, 0)
        assert r.rebate <= bound + slack(bound), (inp, params, r)


def test_property_net_cost_at_least_one_minus_gamma_fee_when_sigma_zero():
    for inp, params in random_cases(3):
        params = replace(params, trap_surcharge_rate=0.0)
        r = compute_settlement(inp, params)
        assert r.net_cost >= (1 - params.rebate_cap) * r.fee - slack(r.fee), (inp, params, r)
        assert r.surcharge == 0


def test_property_net_cost_identity_and_fee_formula():
    for inp, params in random_cases(4):
        r = compute_settlement(inp, params)
        assert r.net_cost == pytest.approx(r.fee - r.rebate + r.surcharge, rel=1e-9, abs=slack(r.fee))
        assert r.fee == pytest.approx(params.credits_per_gco2e * inp.energy_kwh * inp.i_run, rel=1e-9, abs=TINY)
        assert r.rebate >= 0 and r.surcharge >= 0


def test_property_rebate_never_increases_with_k():
    rng = random.Random(5)
    for inp, params in random_cases(5):
        k_small = rng.uniform(0, 3)
        k_big = k_small + rng.uniform(0, 3)
        small = compute_settlement(inp, replace(params, k_sigma=k_small))
        big = compute_settlement(inp, replace(params, k_sigma=k_big))
        assert big.rebate <= small.rebate + slack(small.rebate), (inp, params, k_small, k_big)


def test_property_zone_matches_case_split():
    seen = set()
    for inp, params in random_cases(6):
        r = compute_settlement(inp, params)
        assert r.zone in ZONES
        assert r.saved_low_g == pytest.approx(r.saved_g - params.k_sigma * r.saved_unc_g, rel=1e-9, abs=TINY)
        assert r.saved_high_g == pytest.approx(r.saved_g + params.k_sigma * r.saved_unc_g, rel=1e-9, abs=TINY)
        if r.saved_low_g > 0:
            expected = "saving"
        elif r.saved_high_g < 0:
            expected = "backfire"
        else:
            expected = "dead_zone"
        assert r.zone == expected, (inp, params, r)
        if r.zone != "backfire":
            assert r.surcharge == 0
        seen.add(r.zone)
    assert seen == set(ZONES)  # the random cases really exercise all three zones


def test_property_rebate_is_continuous_at_the_dead_zone_edge():
    """As saved_low -> 0 from above, the rebate -> 0 (no cliff to exploit).

    With dE = 0, saved_unc = E * di_unc does not depend on i_submit, so we can
    choose i_submit to put saved_low exactly at eps:
        E * di - k * E * di_unc = eps   =>   di = k * di_unc + eps / E
    """
    rng = random.Random(7)
    for _ in range(200):
        e = rng.uniform(1e-5, 1e-2)
        i_run, i_run_unc = rng.uniform(50, 800), rng.uniform(0, 50)
        i_submit_unc = rng.uniform(0, 50)
        params = replace(random_params(rng), rebate_cap=0.6)
        di_unc = (i_submit_unc**2 + i_run_unc**2) ** 0.5
        previous = None
        for eps in (1e-3, 1e-5, 1e-7, 1e-9):
            i_submit = i_run + params.k_sigma * di_unc + eps / e
            r = compute_settlement(SettlementInputs(e, 0.0, i_submit, i_submit_unc, i_run, i_run_unc), params)
            assert r.zone == "saving"
            bound = params.rebate_rate * params.credits_per_gco2e * eps
            assert r.rebate <= bound * (1 + 1e-6) + 1e-9
            if previous is not None:
                assert r.rebate <= previous + TINY
            previous = r.rebate
        assert previous < 1e-3  # tiny proven saving -> tiny rebate


# ---------------------------------------------------------------- 3. validation
GOOD = inputs(700, 70, 80, 8)


@pytest.mark.parametrize(
    "field", ["energy_kwh", "energy_unc_kwh", "i_submit", "i_submit_unc", "i_run", "i_run_unc"]
)
def test_negative_inputs_raise(field):
    with pytest.raises(ValueError):
        compute_settlement(replace(GOOD, **{field: -1e-9}))


@pytest.mark.parametrize(
    "change",
    [
        {"credits_per_gco2e": 0.0},
        {"credits_per_gco2e": -1.0},
        {"rebate_rate": 0.0},
        {"rebate_rate": -0.1},
        {"rebate_rate": 1.01},
        {"rebate_cap": 1.0},
        {"rebate_cap": 1.5},
        {"rebate_cap": -0.01},
        {"k_sigma": -0.5},
        {"trap_surcharge_rate": -0.1},
    ],
    ids=lambda c: next(iter(c.items())).__repr__(),
)
def test_bad_params_raise(change):
    with pytest.raises(ValueError):
        compute_settlement(GOOD, replace(SettlementParams(), **change))


def test_boundary_values_are_valid():
    """The edges of the allowed ranges must NOT raise."""
    zero = SettlementInputs(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    r = compute_settlement(zero)
    assert r.fee == 0 and r.rebate == 0 and r.surcharge == 0 and r.zone == "dead_zone"
    edges = SettlementParams(rebate_rate=1.0, rebate_cap=0.0, k_sigma=0.0, trap_surcharge_rate=0.0)
    r = compute_settlement(GOOD, edges)
    assert r.rebate == 0  # gamma = 0 means no rebate can ever be paid
