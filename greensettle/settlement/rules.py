"""The settlement rule: how many credits a finished job costs.

Units used throughout this file:
  energy        kWh
  intensity     g CO2e per kWh
  emissions     g CO2e
  credits       "credits", with p = credits_per_gco2e (default 1000), so that
                1 credit = 1 mg CO2e

This module is pure arithmetic: no clock, no database, no I/O. The ledger
(greensettle/ledger) turns a SettlementResult into ledger entries.
"""

from __future__ import annotations

from dataclasses import dataclass

ZONES = ("saving", "dead_zone", "backfire")


@dataclass(frozen=True)
class SettlementInputs:
    energy_kwh: float  # E: job energy (Phase 1: energy_kwh.job)
    energy_unc_kwh: float  # dE: Phase 1 uncertainty_kwh.combined
    i_submit: float  # intensity when the job was SUBMITTED = counterfactual "run immediately"
    i_submit_unc: float
    i_run: float  # intensity during the run (Phase 1 intensity.g_per_kwh, job midpoint)
    i_run_unc: float


@dataclass(frozen=True)
class SettlementParams:
    credits_per_gco2e: float = 1000.0  # p
    rebate_rate: float = 0.5  # rho, in (0, 1]
    rebate_cap: float = 0.6  # gamma, in [0, 1): rebate may never exceed gamma x fee
    k_sigma: float = 1.0  # k >= 0: dead-zone width in standard uncertainties; 0 disables the dead zone
    trap_surcharge_rate: float = 0.0  # sigma >= 0: extra charge for a PROVEN backfire (default OFF, see docs)


@dataclass(frozen=True)
class SettlementResult:
    fee: float  # credits
    rebate: float  # credits
    surcharge: float  # credits
    net_cost: float  # credits
    saved_g: float  # g CO2e saved versus running immediately (negative = more emitted)
    saved_unc_g: float  # standard uncertainty of saved_g
    saved_low_g: float  # saved_g - k * saved_unc_g
    saved_high_g: float  # saved_g + k * saved_unc_g
    zone: str  # "saving" | "dead_zone" | "backfire"


def compute_settlement(inputs: SettlementInputs, params: SettlementParams = SettlementParams()) -> SettlementResult:
    """Settle one finished job: fee for what it emitted, rebate for proven savings.

    Symbols: E = inputs.energy_kwh, dE = inputs.energy_unc_kwh,
    p = credits_per_gco2e, rho = rebate_rate, gamma = rebate_cap,
    k = k_sigma, sigma = trap_surcharge_rate.

    Validation (raise ValueError, before computing anything):
      - every field of `inputs` must be >= 0
        (energy, energy uncertainty, both intensities, both intensity uncertainties)
      - p > 0
      - 0 < rho <= 1
      - 0 <= gamma < 1
      - k >= 0
      - sigma >= 0

    Steps:
      1. fee = p * E * i_run
           The fee charges the ACTUAL emissions of the run.
      2. di     = i_submit - i_run
         di_unc = hypot(i_submit_unc, i_run_unc)
           The two intensity errors are independent, so they add in quadrature.
      3. saved_g     = E * di
         saved_unc_g = hypot(di * dE, E * di_unc)
           Grams saved by running now instead of at submission time, and the
           standard uncertainty of that product (relative errors in quadrature,
           written without division so it also works when E or di is 0).
      4. saved_low_g  = saved_g - k * saved_unc_g
         saved_high_g = saved_g + k * saved_unc_g
      5. Exactly one zone applies:
           if saved_low_g > 0:
               zone = "saving"
               rebate = min(rho * p * saved_low_g, gamma * fee)
               surcharge = 0
               (pay only on the PROVEN lower bound, and never more than gamma x fee)
           elif saved_high_g < 0:
               zone = "backfire"
               rebate = 0
               surcharge = sigma * p * (-saved_high_g)
               (charge only the PROVEN part of the extra emissions)
           else:
               zone = "dead_zone"
               rebate = 0
               surcharge = 0
               (the saving or loss is inside the measurement noise)
      6. net_cost = fee - rebate + surcharge

    Return a SettlementResult with every field filled in (fee, rebate,
    surcharge, net_cost, saved_g, saved_unc_g, saved_low_g, saved_high_g,
    zone), all as plain floats in credits or grams.

    Properties the tests check:
      - rebate <= gamma * fee
      - rebate == 0 whenever saved_low_g <= 0
      - with sigma = 0: net_cost >= (1 - gamma) * fee  (nobody can farm credits)
      - net_cost == fee - rebate + surcharge
      - increasing k never increases the rebate
      - the rebate goes to 0 continuously as saved_low_g goes to 0 from above
        (no cliff at the edge of the dead zone)
      - k = 0 disables the dead zone (the "naive ex-post" comparison scheme)
    """
    raise NotImplementedError("Phase 2: write compute_settlement yourself, see the docstring")
