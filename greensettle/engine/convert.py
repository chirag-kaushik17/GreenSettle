"""Energy (kWh) x carbon intensity (g/kWh) -> emissions (gCO2e), with an error band."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Emissions:
    value_g: float  # grams of CO2-equivalent
    unc_g: float  # absolute uncertainty (+/- grams)


def to_gco2e(energy_kwh: float, energy_unc_kwh: float, g_per_kwh: float, unc_g_per_kwh: float) -> Emissions:
    """gCO2e = E * I.

    For a product, RELATIVE errors add in quadrature:
        unc / value = sqrt( (dE / E)^2 + (dI / I)^2 )
    Multiplying both sides by value = E * I gives the same thing without any
    division, which also works when E or I is zero:
        unc = sqrt( (I * dE)^2 + (E * dI)^2 )
    """
    value = energy_kwh * g_per_kwh
    unc = math.hypot(g_per_kwh * energy_unc_kwh, energy_kwh * unc_g_per_kwh)
    return Emissions(value_g=value, unc_g=unc)
