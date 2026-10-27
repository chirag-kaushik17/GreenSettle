"""Carbon intensity of electricity: grams of CO2-equivalent per kWh.

Every provider has the same interface:

    provider.get(timestamp) -> IntensityReading(g_per_kwh, unc_g_per_kwh, source)

IntensityReading is a NamedTuple, so it can also be unpacked like a plain
tuple:  g, unc, source = provider.get(ts)

Phase 1 ships SimulatedCurve (the default). It is NOT real grid data: it is a
made-up but plausible daily shape, used so the rest of the system can be
built and demoed without an API key.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Protocol

log = logging.getLogger(__name__)

FORCE_STATES = ("green", "dirty")
STATE_FILENAME = "intensity_state.json"


class IntensityReading(NamedTuple):
    g_per_kwh: float
    unc_g_per_kwh: float
    source: str


class IntensityProvider(Protocol):
    def get(self, timestamp: datetime) -> IntensityReading: ...


class SimulatedCurve:
    """Deterministic, made-up daily carbon-intensity curve.

    intensity(t) = base
                   - solar dip (a bell curve centred on early afternoon)
                   + wind-like variation (three slow sine waves)
    and never below `floor_g`.

    The wind waves' periods and phases come from random.Random(seed), so the
    same seed always gives exactly the same curve (reproducible demos/tests).
    The hour of day is taken in the computer's LOCAL time zone, so the solar
    dip happens around local midday.

    force_state("green" | "dirty") pins the value for demos; None goes back
    to the curve.
    """

    GREEN_G_PER_KWH = 80.0  # roughly a windy/sunny low-carbon grid
    DIRTY_G_PER_KWH = 700.0  # roughly a coal-heavy grid

    def __init__(
        self,
        seed: int = 42,
        base_g: float = 450.0,
        solar_dip_g: float = 220.0,
        solar_peak_hour: float = 13.0,
        solar_width_h: float = 2.5,
        wind_amplitude_g: float = 60.0,
        relative_unc: float = 0.10,  # PLACEHOLDER: +/-10 % on every simulated value
        floor_g: float = 50.0,
    ) -> None:
        self.seed = seed
        self.base_g = base_g
        self.solar_dip_g = solar_dip_g
        self.solar_peak_hour = solar_peak_hour
        self.solar_width_h = solar_width_h
        self.wind_amplitude_g = wind_amplitude_g
        self.relative_unc = relative_unc
        self.floor_g = floor_g
        rng = random.Random(seed)
        # Three (period in hours, phase in radians) pairs for the "wind".
        self._wind_waves = [(rng.uniform(6.0, 36.0), rng.uniform(0.0, 2 * math.pi)) for _ in range(3)]
        self._forced: str | None = None

    @property
    def forced_state(self) -> str | None:
        return self._forced

    def force_state(self, state: str | None) -> None:
        if state is not None and state not in FORCE_STATES:
            raise ValueError(f"state must be one of {FORCE_STATES} or None, not {state!r}")
        self._forced = state

    def curve_value(self, timestamp: datetime) -> float:
        """The un-forced curve value in g/kWh."""
        local = timestamp.astimezone()  # naive datetimes are treated as local time
        hour = local.hour + local.minute / 60 + local.second / 3600
        solar = self.solar_dip_g * math.exp(-((hour - self.solar_peak_hour) ** 2) / (2 * self.solar_width_h**2))
        hours_since_epoch = timestamp.timestamp() / 3600
        wind = self.wind_amplitude_g * sum(
            math.sin(2 * math.pi * hours_since_epoch / period + phase) for period, phase in self._wind_waves
        ) / len(self._wind_waves)
        return max(self.floor_g, self.base_g - solar + wind)

    def get(self, timestamp: datetime) -> IntensityReading:
        if self._forced == "green":
            g, source = self.GREEN_G_PER_KWH, "simulated:forced-green"
        elif self._forced == "dirty":
            g, source = self.DIRTY_G_PER_KWH, "simulated:forced-dirty"
        else:
            g, source = self.curve_value(timestamp), f"simulated:curve(seed={self.seed})"
        return IntensityReading(g, g * self.relative_unc, source)


# --------------------------------------------------------------------------
# Demo override, persisted so that every CLI command sees it.
# --------------------------------------------------------------------------
def load_forced_state(data_dir: Path) -> str | None:
    path = data_dir / STATE_FILENAME
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8")).get("forced")
    except (json.JSONDecodeError, AttributeError):
        log.warning("ignoring unreadable %s", path)
        return None
    return state if state in FORCE_STATES else None


def save_forced_state(data_dir: Path, state: str | None) -> Path:
    if state is not None and state not in FORCE_STATES:
        raise ValueError(f"state must be one of {FORCE_STATES} or None")
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / STATE_FILENAME
    path.write_text(json.dumps({"forced": state}), encoding="utf-8")
    return path


def make_provider(name: str, data_dir: Path) -> IntensityProvider:
    """Build the provider chosen on the command line, applying any demo override."""
    if name == "simulated":
        curve = SimulatedCurve()
        curve.force_state(load_forced_state(data_dir))
        return curve
    raise ValueError(f"unknown intensity provider {name!r}")
