"""Turn CPU power readings into energy.

Sections in this file (read top to bottom):
  1. MeterConfig       - every tunable number in ONE place
  2. Sampler           - background thread that polls watts every ~1 s
  3. Integration       - watts over time -> joules -> kWh (trapezoidal rule)
  4. Idle calibration  - how many watts the CPU draws doing "nothing"
  5. CPU-time tracking - how much CPU the job (and its children) used
  6. Attribution       - which share of the extra energy belongs to the job
  7. Uncertainty       - how wrong could the job's energy be (kWh, +/-)
  8. Job runner        - ties 2-7 together around one subprocess

Units: time in seconds, power in watts (W), energy in joules (J) or
kilowatt-hours (kWh). 1 W for 1 s = 1 J, and 1 kWh = 3,600,000 J.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import subprocess
import threading
import uuid
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import psutil

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


# --------------------------------------------------------------------------
# 5. CPU-time tracking (psutil)
# --------------------------------------------------------------------------
class ProcessTreeCpuTracker:
    """Adds up user+system CPU seconds of a process AND all its descendants.

    A job such as `python tools/burn.py --threads 4` starts child processes
    that may finish before the job does. Once a process has exited we can no
    longer ask the OS about it, so we remember the LAST value we saw for every
    process and keep it in the total.

    Processes are keyed by (pid, create_time) so that a PID reused by the OS
    for an unrelated new process is not confused with the old one.

    Limitations: a child that starts AND exits between two polls (default
    0.25 s apart) is never seen, and the last <= 0.25 s of every process is
    missed. run_job corrects for this with the OS's final numbers where the
    OS provides them (see _reap_if_exited).
    """

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self._last_seen: dict[tuple[int, float], float] = {}

    def poll(self) -> None:
        try:
            root = psutil.Process(self.root_pid)
            processes = [root, *root.children(recursive=True)]
        except psutil.Error:  # root already gone: keep the values we have
            return
        for proc in processes:
            try:
                with proc.oneshot():
                    key = (proc.pid, proc.create_time())
                    times = proc.cpu_times()
            except psutil.Error:  # exited (or not accessible) between listing and reading
                continue
            self._last_seen[key] = times.user + times.system

    @property
    def total_cpu_s(self) -> float:
        return sum(self._last_seen.values())

    @property
    def processes_seen(self) -> int:
        return len(self._last_seen)


def system_busy_cpu_seconds() -> float:
    """CPU-seconds the WHOLE machine has spent busy since boot (all cores summed).

    busy = total - idle. Same definition psutil uses for cpu_percent():
    iowait counts as idle, and on Linux guest time is already inside user time
    so it is subtracted to avoid counting it twice. On Windows the fields are
    user, system, idle, interrupt, dpc.
    """
    t = psutil.cpu_times()
    total = sum(t)
    total -= getattr(t, "guest", 0.0) + getattr(t, "guest_nice", 0.0)
    idle = t.idle + getattr(t, "iowait", 0.0)
    return total - idle


# --------------------------------------------------------------------------
# 6. Attribution: how much of the measured energy belongs to the job?
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EnergySplit:
    package_total_j: float  # E_pkg: everything the CPU package used in the window
    idle_baseline_j: float  # E_idle: what it would have used doing nothing
    dynamic_j: float  # E_dynamic: the extra energy caused by ALL activity
    cpu_share: float  # job CPU-seconds / all busy CPU-seconds, clamped to [0, 1]
    platform_overhead_j: float  # optional platform estimate attributed to the job (0 if off)
    job_j: float  # E_job: the part we charge to this job


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def split_energy(
    package_j: float,
    idle_w: float,
    duration_s: float,
    job_cpu_s: float,
    system_busy_cpu_s: float,
    platform_overhead_w: float = 0.0,
) -> EnergySplit:
    """Split measured package energy into idle, dynamic and the job's part.

        E_idle    = idle_power * duration
        E_dynamic = max(0, E_pkg - E_idle)     (cannot be negative)
        cpu_share = clamp(job_cpu_s / system_busy_cpu_s, 0, 1)
        E_job     = E_dynamic * cpu_share  (+ optional platform overhead * duration * cpu_share)

    Why only the DYNAMIC part? The idle energy would have been spent anyway,
    job or no job, so it is not caused by the job.

    KNOWN LIMITATION: cpu_share treats every CPU-second as equal. On a hybrid
    Intel Core Ultra, a second on a P-core costs noticeably more energy than a
    second on an E-core or low-power E-core, so a job that ran mostly on
    P-cores is under-charged (and vice versa). The attribution uncertainty
    term (MeterConfig.attribution_base_fraction) is meant to cover this until
    a later phase can measure it.
    """
    idle_j = idle_w * duration_s
    dynamic_j = max(0.0, package_j - idle_j)
    # Job CPU time can slightly exceed system busy time because the two are
    # read at slightly different moments; and if nothing was busy, share is 0.
    share = clamp(job_cpu_s / system_busy_cpu_s, 0.0, 1.0) if system_busy_cpu_s > 0 else 0.0
    platform_j = platform_overhead_w * duration_s * share
    return EnergySplit(
        package_total_j=package_j,
        idle_baseline_j=idle_j,
        dynamic_j=dynamic_j,
        cpu_share=share,
        platform_overhead_j=platform_j,
        job_j=dynamic_j * share + platform_j,
    )


# --------------------------------------------------------------------------
# 7. Uncertainty (kept separate so it can be explained and tested on its own)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UncertaintyBreakdown:
    """Absolute uncertainties in kWh (each is a +/- amount)."""

    sensor_kwh: float
    sampling_kwh: float
    idle_baseline_kwh: float
    attribution_kwh: float
    platform_kwh: float
    combined_kwh: float


def combine_in_quadrature(*terms: float) -> float:
    """sqrt(a^2 + b^2 + ...): the standard way to add INDEPENDENT errors.

    Independent errors partly cancel out, so the total is smaller than the
    plain sum but at least as large as the biggest single term.
    """
    return math.sqrt(sum(t * t for t in terms))


def estimate_uncertainty(
    package_kwh: float,
    package_left_riemann_kwh: float,
    idle_std_w: float,
    duration_s: float,
    dynamic_kwh: float,
    cpu_share: float,
    config: MeterConfig = MeterConfig(),
) -> UncertaintyBreakdown:
    """Four (optionally five) independent error sources, combined in quadrature.

      sensor      = cpu_share * sensor_tolerance_fraction * E_pkg
      sampling    = cpu_share * |E_trapezoid - E_left_riemann|
      idle        = cpu_share * idle_std * duration
      attribution = (base + k * (1 - cpu_share)) * E_dynamic
      platform    = platform_overhead_unc_w * duration * cpu_share   (0 unless enabled)

    Why the first three terms are multiplied by cpu_share: they are errors in
    E_pkg and E_idle, and E_job = cpu_share * (E_pkg - E_idle). An error of
    X joules in E_pkg therefore moves E_job by only cpu_share * X. (Without
    this, a job owning 5 % of the CPU would get an error band several times
    larger than its own energy.) The attribution term is already about the
    split itself, so it is not scaled again.
    The coefficients come from MeterConfig and are PLACEHOLDERS.
    """
    sensor = cpu_share * config.sensor_tolerance_fraction * package_kwh
    sampling = cpu_share * abs(package_kwh - package_left_riemann_kwh)
    idle = cpu_share * joules_to_kwh(idle_std_w * duration_s)
    attribution = (config.attribution_base_fraction + config.attribution_k * (1 - cpu_share)) * dynamic_kwh
    platform = (
        joules_to_kwh(config.platform_overhead_unc_w * duration_s * cpu_share)
        if config.platform_overhead_w > 0
        else 0.0
    )
    return UncertaintyBreakdown(
        sensor_kwh=sensor,
        sampling_kwh=sampling,
        idle_baseline_kwh=idle,
        attribution_kwh=attribution,
        platform_kwh=platform,
        combined_kwh=combine_in_quadrature(sensor, sampling, idle, attribution, platform),
    )


# --------------------------------------------------------------------------
# 8. Job runner
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class JobMeasurement:
    job_id: str
    command: list[str]
    exit_code: int
    started_at: str  # wall clock, ISO-8601 UTC (for humans and later phases)
    ended_at: str
    duration_s: float  # from monotonic sample timestamps (first to last sample)
    sampler: SamplerStats
    split: EnergySplit
    job_cpu_s: float
    system_busy_cpu_s: float
    uncertainty: UncertaintyBreakdown
    midpoint_utc: datetime  # used to look up carbon intensity


def _reap_if_exited(proc: subprocess.Popen) -> tuple[bool, float]:
    """Check (without blocking) whether the job's root process has exited.

    Returns (exited, os_cpu_s). os_cpu_s is the operating system's own FINAL
    count of CPU seconds, or 0.0 when the OS cannot tell us.

    Why this exists: the psutil tracker only sees a process while it is alive,
    so the last <= cpu_poll_s of every process is lost, and once a process has
    exited psutil cannot read it any more (polling again after exit adds
    nothing; this was tested). On Linux/macOS, os.wait4() reaps the process
    AND returns its resource usage, which includes every descendant it waited
    for (e.g. the multiprocessing workers that burn.py joins). Windows has no
    wait4, so there we return 0.0 and rely on the tracker alone.
    """
    if hasattr(os, "wait4"):
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid == 0:  # still running
            return False, 0.0
        proc.returncode = os.waitstatus_to_exitcode(status)  # we reaped it, so tell Popen
        return True, usage.ru_utime + usage.ru_stime
    return proc.poll() is not None, 0.0


def new_job_id() -> str:
    return datetime.now(timezone.utc).strftime("job-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]


def run_job(
    command: Sequence[str],
    read_watts: Callable[[], float],
    idle: IdleCalibration,
    config: MeterConfig = MeterConfig(),
    cpu_poll_s: float = 0.25,
) -> JobMeasurement:
    """Run `command` as a subprocess and measure it.

    Order of events:
      1. start the sampler (first power sample) and snapshot system CPU time
      2. launch the command; poll its process tree for CPU time every 0.25 s
      3. when it exits: snapshot system CPU time, stop the sampler (last sample)
      4. integrate, split, estimate uncertainty
    """
    if not command:
        raise ValueError("No command given to run.")
    job_id = new_job_id()
    sampler = Sampler(read_watts, config.poll_interval_s)
    started_wall = datetime.now(timezone.utc)
    sampler.start()
    busy_before = system_busy_cpu_seconds()
    try:
        proc = subprocess.Popen(list(command))
    except OSError:
        sampler.stop(final_sample=False)
        raise
    tracker = ProcessTreeCpuTracker(proc.pid)
    os_cpu_s = 0.0
    try:
        while True:
            # The last poll before exit can be up to cpu_poll_s old, so the
            # tracker misses the final slice of each process (see _reap_if_exited).
            tracker.poll()
            exited, os_cpu_s = _reap_if_exited(proc)
            if exited:
                break
            time.sleep(cpu_poll_s)
    except KeyboardInterrupt:
        log.warning("interrupted: stopping the job")
        proc.terminate()
        proc.wait()
        raise
    finally:
        busy_after = system_busy_cpu_seconds()
        sampler.stop()
    ended_wall = datetime.now(timezone.utc)

    samples = sampler.samples
    if len(samples) < 2:
        raise RuntimeError("Fewer than 2 power samples: cannot integrate energy.")
    duration_s = samples[-1].t - samples[0].t
    package_j = integrate_trapezoid(samples)
    package_left_j = integrate_left_riemann(samples)
    system_busy = busy_after - busy_before
    # Both numbers are LOWER bounds of the true job CPU time: the tracker misses
    # each process's last poll interval, and the OS figure misses descendants
    # that nobody waited for (orphans). So the larger one is the better estimate.
    job_cpu_s = max(tracker.total_cpu_s, os_cpu_s)
    split = split_energy(
        package_j, idle.mean_w, duration_s, job_cpu_s, system_busy, config.platform_overhead_w
    )
    unc = estimate_uncertainty(
        package_kwh=joules_to_kwh(package_j),
        package_left_riemann_kwh=joules_to_kwh(package_left_j),
        idle_std_w=idle.std_w,
        duration_s=duration_s,
        dynamic_kwh=joules_to_kwh(split.dynamic_j),
        cpu_share=split.cpu_share,
        config=config,
    )
    stats = sampler.stats()
    if stats.unchanged_fraction > 0.5:
        log.info("%.0f%% of polls repeated the previous value (LHM refresh slower than our poll?)",
                 100 * stats.unchanged_fraction)
    if proc.returncode != 0:
        log.warning("job exited with code %d", proc.returncode)
    return JobMeasurement(
        job_id=job_id,
        command=list(command),
        exit_code=proc.returncode,
        started_at=started_wall.isoformat(timespec="seconds"),
        ended_at=ended_wall.isoformat(timespec="seconds"),
        duration_s=duration_s,
        sampler=stats,
        split=split,
        job_cpu_s=job_cpu_s,
        system_busy_cpu_s=system_busy,
        uncertainty=unc,
        midpoint_utc=started_wall + (ended_wall - started_wall) / 2,
    )
