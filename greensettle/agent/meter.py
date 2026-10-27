"""Turn CPU power readings into energy.

Sections in this file (read top to bottom):
  1. MeterConfig       - every tunable number in ONE place
  2. Sampler           - background thread that polls watts every ~1 s
  3. Integration       - watts over time -> joules -> kWh (trapezoidal rule)
  4. Idle calibration  - how many watts the CPU draws doing "nothing"

Units: time in seconds, power in watts (W), energy in joules (J) or
kilowatt-hours (kWh). 1 W for 1 s = 1 J, and 1 kWh = 3,600,000 J.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

JOULES_PER_KWH = 3_600_000.0
DEFAULT_IDLE_PATH = Path("data") / "idle.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 1. Configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MeterConfig:
    """All tunable numbers of the meter.

    IMPORTANT: every coefficient marked PLACEHOLDER is an educated guess, NOT a
    measured fact. Each one must be replaced by a calibration experiment in a
    later phase (e.g. compare against a wall-socket power meter).
    """

    # How often the sampler polls LibreHardwareMonitor. LHM itself refreshes
    # about once per second, so polling much faster only repeats old values.
    poll_interval_s: float = 1.0

    # PLACEHOLDER: warn during idle calibration if std dev > this fraction of
    # the mean (a busy background task makes the baseline unreliable).
    idle_std_warn_fraction: float = 0.25

    # PLACEHOLDER: assumed accuracy of the CPU Package sensor (RAPL-style
    # energy counters), as a fraction of measured package energy.
    sensor_tolerance_fraction: float = 0.05

    # PLACEHOLDER: attribution uncertainty = (base + k * (1 - cpu_share)) * E_dynamic.
    # "base" covers the CPU-time proxy being imperfect even for a lone job
    # (e.g. P-core vs E-core seconds cost different energy); "k" grows the
    # error as the job shares the machine with more other work.
    attribution_base_fraction: float = 0.05
    attribution_k: float = 0.20

    # OPTIONAL, OFF by default (0.0). Extra watts drawn by the rest of the
    # platform (RAM, chipset, ...) while the job runs, which the dead
    # "CPU Memory"/"CPU Platform" sensors cannot show us. If set, it is added
    # to the job's energy (scaled by cpu_share) with its own uncertainty.
    platform_overhead_w: float = 0.0
    platform_overhead_unc_w: float = 0.0  # PLACEHOLDER when the overhead is used


# --------------------------------------------------------------------------
# 2. Sampler
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    t: float  # seconds, from time.monotonic() (never jumps, unlike the wall clock)
    watts: float


@dataclass(frozen=True)
class SamplerStats:
    samples: int
    failed_reads: int
    mean_interval_s: float  # average real gap between samples
    jitter_s: float  # standard deviation of those gaps
    unchanged_fraction: float  # share of polls that returned the same value as the previous one


class Sampler:
    """Polls `read_watts()` on a background thread every `interval_s` seconds.

    Usage:
        sampler = Sampler(client.package_watts, interval_s=1.0)
        sampler.start()      # takes the first sample immediately
        ...                  # do the work being measured
        sampler.stop()       # takes one last sample, then returns
        samples = sampler.samples
    """

    def __init__(self, read_watts: Callable[[], float], interval_s: float = 1.0) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._read_watts = read_watts
        self.interval_s = interval_s
        self._samples: list[Sample] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.failed_reads = 0
        self._final_sample_taken = False

    def _take_sample(self) -> None:
        t_before = time.monotonic()
        try:
            watts = self._read_watts()
        except Exception as exc:  # one failed poll must not kill the whole measurement
            self.failed_reads += 1
            log.warning("sensor read failed (%s); skipping this sample", exc)
            return
        t_after = time.monotonic()
        # The HTTP request takes a few ms; stamp the sample at its midpoint.
        sample = Sample(t=(t_before + t_after) / 2, watts=watts)
        with self._lock:
            self._samples.append(sample)

    def _run(self) -> None:
        # Schedule against fixed ticks (start + n * interval) so small delays
        # do not accumulate into drift.
        next_tick = time.monotonic() + self.interval_s
        while not self._stop_event.wait(max(0.0, next_tick - time.monotonic())):
            self._take_sample()
            next_tick += self.interval_s
            if next_tick < time.monotonic():  # fell behind (slow server): resync
                next_tick = time.monotonic() + self.interval_s

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Sampler already started")
        self._take_sample()  # sample at t=0 so the window starts with a reading
        self._thread = threading.Thread(target=self._run, name="greensettle-sampler", daemon=True)
        self._thread.start()

    def stop(self, final_sample: bool = True) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        if final_sample:
            self._take_sample()  # sample at the very end of the window
            self._final_sample_taken = True

    @property
    def samples(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def stats(self) -> SamplerStats:
        """Timing diagnostics. The extra sample taken by stop() is off-schedule
        (it can come just after the previous one), so it is left out here; it
        IS still used for energy integration."""
        samples = self.samples
        if self._final_sample_taken and len(samples) > 2:
            samples = samples[:-1]
        return sampler_stats(samples, self.failed_reads)


def sampler_stats(samples: Sequence[Sample], failed_reads: int = 0) -> SamplerStats:
    gaps = [b.t - a.t for a, b in zip(samples, samples[1:])]
    repeats = sum(1 for a, b in zip(samples, samples[1:]) if a.watts == b.watts)
    return SamplerStats(
        samples=len(samples),
        failed_reads=failed_reads,
        mean_interval_s=statistics.fmean(gaps) if gaps else 0.0,
        jitter_s=statistics.pstdev(gaps) if len(gaps) >= 2 else 0.0,
        unchanged_fraction=repeats / len(gaps) if gaps else 0.0,
    )


# --------------------------------------------------------------------------
# 3. Integration: power (W) over time -> energy (J)
# --------------------------------------------------------------------------
def integrate_trapezoid(samples: Sequence[Sample]) -> float:
    """Energy in joules using the trapezoidal rule.

    Between two samples we assume power changed in a straight line, so the
    energy of that slice is the AVERAGE of the two readings times the gap:
        E += (P_i + P_(i+1)) / 2 * (t_(i+1) - t_i)
    """
    return sum((a.watts + b.watts) / 2 * (b.t - a.t) for a, b in zip(samples, samples[1:]))


def integrate_left_riemann(samples: Sequence[Sample]) -> float:
    """Energy in joules assuming power stayed at the EARLIER reading for each slice.

    Only used to estimate sampling error: if the two methods disagree a lot,
    power was changing quickly relative to our sampling interval.
    """
    return sum(a.watts * (b.t - a.t) for a, b in zip(samples, samples[1:]))


def joules_to_kwh(joules: float) -> float:
    return joules / JOULES_PER_KWH


# --------------------------------------------------------------------------
# 4. Idle calibration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class IdleCalibration:
    mean_w: float
    std_w: float
    samples: int
    seconds: float
    measured_at: str  # ISO-8601 UTC timestamp

    def save(self, path: Path = DEFAULT_IDLE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_IDLE_PATH) -> "IdleCalibration":
        if not path.exists():
            raise FileNotFoundError(
                f"No idle calibration at {path}. Run `python -m greensettle.cli calibrate-idle` first."
            )
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def calibrate_idle(
    read_watts: Callable[[], float],
    seconds: float = 30,
    config: MeterConfig = MeterConfig(),
    path: Path | None = DEFAULT_IDLE_PATH,
) -> IdleCalibration:
    """Sample CPU Package watts for `seconds` while the machine is idle.

    Leave the laptop alone (plugged in, no other programs) while this runs.
    Saves the result to `path` (data/idle.json) unless path is None.
    """
    sampler = Sampler(read_watts, config.poll_interval_s)
    sampler.start()
    try:
        time.sleep(seconds)
    finally:
        sampler.stop()
    watts = [s.watts for s in sampler.samples]
    if len(watts) < 2:
        raise RuntimeError("Fewer than 2 samples collected; is the sensor server responding?")

    stats = sampler.stats()
    result = IdleCalibration(
        mean_w=statistics.fmean(watts),
        std_w=statistics.stdev(watts),
        samples=len(watts),
        seconds=round(sampler.samples[-1].t - sampler.samples[0].t, 3),
        measured_at=utc_now_iso(),
    )
    log.info(
        "idle: %d samples, mean interval %.3f s, jitter %.3f s, unchanged polls %.0f%%",
        stats.samples, stats.mean_interval_s, stats.jitter_s, 100 * stats.unchanged_fraction,
    )
    if result.std_w > config.idle_std_warn_fraction * result.mean_w:
        log.warning(
            "idle std dev %.2f W is high compared to the mean %.2f W: the machine may not be idle. "
            "Close other programs and calibrate again.", result.std_w, result.mean_w,
        )
    if path is not None:
        result.save(path)
    return result
