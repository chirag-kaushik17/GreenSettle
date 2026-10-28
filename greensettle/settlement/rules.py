"""Settlement rule: turns one measured job into fee, rebate and surcharge (credits)."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SettlementParams:
    p: float = 1000.0      # credits per (kWh x g/kWh); fee = p * E * i_run
    rho: float = 0.5       # share of the certain saving paid back as rebate
    gamma: float = 0.6     # rebate cap as a fraction of the fee, must be < 1
    k: float = 1.0         # dead-zone width in standard uncertainties
    sigma: float = 0.0     # backfire surcharge rate (0 = off)


@dataclass(frozen=True)
class Settlement:
    zone: str              # "saving" | "dead_zone" | "backfire"
    fee: float
    rebate: float
    surcharge: float
    net: float             # fee + surcharge - rebate
    saved_g: float
    saved_unc_g: float


def compute_settlement(energy_kwh, energy_unc_kwh, i_submit, i_submit_unc,
                       i_run, i_run_unc, params: SettlementParams = SettlementParams()) -> Settlement:
    if not (0 <= params.gamma < 1):
        raise ValueError("gamma must be in [0, 1) so the rebate can never exceed the fee")
    if params.rho < 0 or params.k < 0 or params.sigma < 0 or params.p <= 0:
        raise ValueError("p must be > 0; rho, k, sigma must be >= 0")
    for v in (energy_kwh, energy_unc_kwh, i_submit, i_submit_unc, i_run, i_run_unc):
        if not math.isfinite(v) or v < 0:
            raise ValueError("inputs must be finite and >= 0")

    # 1. What the user pays for: the real emissions of the run.
    fee = params.p * energy_kwh * i_run

    # 2. Grams saved versus running immediately, and how uncertain that is.
    saved = energy_kwh * (i_submit - i_run)
    d = i_submit - i_run
    saved_unc = math.sqrt((energy_unc_kwh * d) ** 2           # energy error
                          + (energy_kwh * i_submit_unc) ** 2   # counterfactual intensity error
                          + (energy_kwh * i_run_unc) ** 2)     # actual intensity error

    # 3. Dead zone: act only on what exceeds k standard uncertainties.
    saved_low = saved - params.k * saved_unc      # pessimistic saving
    extra_low = -saved - params.k * saved_unc     # pessimistic extra emissions

    rebate = surcharge = 0.0
    if saved_low > 0:
        zone = "saving"
        rebate = min(params.rho * params.p * saved_low, params.gamma * fee)
    elif extra_low > 0:
        zone = "backfire"
        surcharge = params.sigma * params.p * extra_low
    else:
        zone = "dead_zone"

    return Settlement(zone, fee, rebate, surcharge, fee + surcharge - rebate, saved, saved_unc)
