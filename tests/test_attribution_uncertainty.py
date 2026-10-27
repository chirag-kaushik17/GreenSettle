"""Attribution (energy split), CPU-time tracking, uncertainty and the job runner."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from greensettle.agent import meter
from greensettle.agent.lhm_client import LhmClient

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- split_energy
def test_split_with_faked_cpu_times():
    # 600 J measured over 30 s, idle 5 W -> idle 150 J, dynamic 450 J; job used 30 of 40 busy s
    s = meter.split_energy(package_j=600, idle_w=5, duration_s=30, job_cpu_s=30, system_busy_cpu_s=40)
    assert s.idle_baseline_j == pytest.approx(150)
    assert s.dynamic_j == pytest.approx(450)
    assert s.cpu_share == pytest.approx(0.75)
    assert s.job_j == pytest.approx(337.5)
    assert s.platform_overhead_j == 0.0


def test_cpu_share_is_clamped_to_one():
    s = meter.split_energy(600, 5, 30, job_cpu_s=45, system_busy_cpu_s=40)
    assert s.cpu_share == 1.0
    assert s.job_j == pytest.approx(s.dynamic_j)


def test_cpu_share_is_clamped_to_zero_and_handles_no_busy_time():
    assert meter.split_energy(600, 5, 30, job_cpu_s=-1, system_busy_cpu_s=40).cpu_share == 0.0
    s = meter.split_energy(600, 5, 30, job_cpu_s=10, system_busy_cpu_s=0)
    assert s.cpu_share == 0.0 and s.job_j == 0.0


def test_dynamic_energy_never_negative():
    s = meter.split_energy(package_j=100, idle_w=5, duration_s=30, job_cpu_s=1, system_busy_cpu_s=1)
    assert s.dynamic_j == 0.0 and s.job_j == 0.0


def test_optional_platform_overhead():
    s = meter.split_energy(600, 5, 30, 30, 40, platform_overhead_w=2.0)
    assert s.platform_overhead_j == pytest.approx(2.0 * 30 * 0.75)
    assert s.job_j == pytest.approx(337.5 + 45)


# ---------------------------------------------------------------- uncertainty
def test_combine_in_quadrature():
    assert meter.combine_in_quadrature(3, 4) == pytest.approx(5)
    assert meter.combine_in_quadrature(1, 2, 2) == pytest.approx(3)
    assert meter.combine_in_quadrature() == 0.0


BASE = dict(package_kwh=1e-4, package_left_riemann_kwh=0.98e-4, idle_std_w=0.3,
            duration_s=30, dynamic_kwh=6e-5, cpu_share=0.8)


def unc(**overrides):
    return meter.estimate_uncertainty(**{**BASE, **overrides})


def test_uncertainty_terms_match_formulas():
    cfg = meter.MeterConfig()
    u = unc()
    assert u.sensor_kwh == pytest.approx(cfg.sensor_tolerance_fraction * 1e-4)
    assert u.sampling_kwh == pytest.approx(0.02e-4)
    assert u.idle_baseline_kwh == pytest.approx(0.3 * 30 / 3.6e6)
    assert u.attribution_kwh == pytest.approx((cfg.attribution_base_fraction + cfg.attribution_k * 0.2) * 6e-5)
    assert u.platform_kwh == 0.0
    assert u.combined_kwh == pytest.approx(
        meter.combine_in_quadrature(u.sensor_kwh, u.sampling_kwh, u.idle_baseline_kwh, u.attribution_kwh)
    )
    assert u.combined_kwh >= max(u.sensor_kwh, u.sampling_kwh, u.idle_baseline_kwh, u.attribution_kwh)


@pytest.mark.parametrize(
    "change, term",
    [
        (dict(package_kwh=2e-4), "sensor_kwh"),  # more energy -> bigger sensor error
        (dict(package_left_riemann_kwh=0.90e-4), "sampling_kwh"),  # methods disagree more
        (dict(idle_std_w=1.0), "idle_baseline_kwh"),  # noisier idle
        (dict(duration_s=60), "idle_baseline_kwh"),  # longer job
        (dict(cpu_share=0.2), "attribution_kwh"),  # job is a smaller part of the load
        (dict(dynamic_kwh=1e-4), "attribution_kwh"),  # more energy to split
    ],
)
def test_each_term_grows_in_the_right_direction(change, term):
    before, after = unc(), unc(**change)
    assert getattr(after, term) > getattr(before, term)
    assert after.combined_kwh > before.combined_kwh


def test_attribution_error_smallest_when_job_owns_the_whole_cpu():
    assert unc(cpu_share=1.0).attribution_kwh < unc(cpu_share=0.5).attribution_kwh


def test_platform_term_only_when_enabled():
    cfg = meter.MeterConfig(platform_overhead_w=2.0, platform_overhead_unc_w=1.0)
    u = meter.estimate_uncertainty(**BASE, config=cfg)
    assert u.platform_kwh == pytest.approx(1.0 * 30 * 0.8 / 3.6e6)


# ---------------------------------------------------------------- CPU-time tracking (real processes)
CHILD_SCRIPT = """
import subprocess, sys, time
burn = "import time\\nt=time.process_time()\\nwhile time.process_time()-t<0.6: pass"
subprocess.run([sys.executable, "-c", burn])   # child burns ~0.6 CPU-s then EXITS
time.sleep(0.6)                                 # parent keeps living, idle
"""


def test_tracker_keeps_cpu_time_of_children_that_exited():
    proc = subprocess.Popen([sys.executable, "-c", CHILD_SCRIPT])
    tracker = meter.ProcessTreeCpuTracker(proc.pid)
    while proc.poll() is None:
        tracker.poll()
        time.sleep(0.05)
    assert tracker.processes_seen >= 2  # parent + child
    assert tracker.total_cpu_s >= 0.4  # the exited child's CPU time is still counted


def test_system_busy_cpu_seconds_increases():
    before = meter.system_busy_cpu_seconds()
    t = time.process_time()
    while time.process_time() - t < 0.3:
        pass
    assert meter.system_busy_cpu_seconds() - before > 0.1


# ---------------------------------------------------------------- run_job end to end
def test_run_job_with_mock_sensor(mock_lhm):
    mock_lhm.watts_fn = lambda: 30.0  # pretend the CPU draws 30 W during the job
    idle = meter.IdleCalibration(mean_w=5.0, std_w=0.2, samples=30, seconds=30, measured_at="x")
    m = meter.run_job(
        [sys.executable, str(ROOT / "tools" / "burn.py"), "--seconds", "1.5", "--threads", "2"],
        LhmClient(mock_lhm.url).package_watts,
        idle,
        meter.MeterConfig(poll_interval_s=0.2),
    )
    assert m.exit_code == 0
    assert m.duration_s == pytest.approx(1.5, abs=1.5)
    assert m.split.package_total_j == pytest.approx(30.0 * m.duration_s, rel=1e-6)
    assert m.split.dynamic_j == pytest.approx(25.0 * m.duration_s, rel=1e-6)
    assert 0 < m.split.cpu_share <= 1
    assert m.job_cpu_s > 1.0  # two workers busy for 1.5 s each
    assert m.split.job_j > 0
    assert m.uncertainty.combined_kwh > 0
