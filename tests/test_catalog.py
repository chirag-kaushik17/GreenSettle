"""Job catalog: only known job types with integer params inside hard ranges."""

from __future__ import annotations

import subprocess
import sys

import pytest

from greensettle.jobs import catalog
from greensettle.jobs.catalog import CatalogError, build_argv, validate, validate_est_duration


def test_valid_jobs():
    assert validate("burn", {"seconds": 20, "workers": 2}, cpu_count=4) == {"seconds": 20, "workers": 2}
    assert validate("hash", {"megabytes": 1, "repeats": 400}) == {"megabytes": 1, "repeats": 400}
    assert validate("primes", {"limit": 2}) == {"limit": 2}


@pytest.mark.parametrize(
    "job_type, params",
    [
        ("rm", {}),                                         # unknown type
        ("BURN", {"seconds": 1, "workers": 1}),             # names are exact
        (None, {}),
        (["burn"], {}),
        ("burn", [("seconds", 1)]),                         # params must be an object
        ("burn", {"seconds": 1}),                           # missing param
        ("burn", {"seconds": 1, "workers": 1, "cmd": 1}),   # unknown param
        ("burn", {"seconds": 61, "workers": 1}),            # thermal cap
        ("burn", {"seconds": 0, "workers": 1}),
        ("burn", {"seconds": -5, "workers": 1}),
        ("burn", {"seconds": 10, "workers": 0}),
        ("burn", {"seconds": 10, "workers": 5}),            # more workers than CPUs (4 here)
        ("burn", {"seconds": 10.0, "workers": 1}),          # float, not int
        ("burn", {"seconds": "10", "workers": 1}),          # string
        ("burn", {"seconds": True, "workers": 1}),          # bool is not an int here
        ("burn", {"seconds": "10; rm -rf /", "workers": 1}),
        ("hash", {"megabytes": 65, "repeats": 1}),
        ("hash", {"megabytes": 1, "repeats": 401}),
        ("hash", {"megabytes": 0, "repeats": 1}),
        ("primes", {"limit": 1}),
        ("primes", {"limit": 100_000_001}),
        ("primes", {"limit": None}),
    ],
)
def test_invalid_jobs_rejected(job_type, params):
    with pytest.raises(CatalogError):
        validate(job_type, params, cpu_count=4)


def test_build_argv_is_fixed_and_uses_this_python():
    argv = build_argv("burn", {"workers": 2, "seconds": 20}, cpu_count=4)
    assert argv == [sys.executable, "-m", "greensettle.jobs.burn", "--seconds", "20", "--workers", "2"]
    assert all(isinstance(a, str) for a in argv)
    with pytest.raises(CatalogError):
        build_argv("burn", {"seconds": 99, "workers": 1}, cpu_count=4)


@pytest.mark.parametrize("value", [0, -1, 120.0001, 1e9, float("nan"), "25", None, True])
def test_est_duration_limits(value):
    with pytest.raises(CatalogError):
        validate_est_duration(value)


def test_est_duration_ok():
    assert validate_est_duration(25) == 25.0
    assert validate_est_duration(catalog.MAX_EST_DURATION_S) == 120.0


def test_job_modules_run_and_recheck_their_arguments():
    ok = subprocess.run(build_argv("primes", {"limit": 1000}), capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "168 primes below 1000" in ok.stdout
    ok = subprocess.run(build_argv("hash", {"megabytes": 1, "repeats": 1}), capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "sha256" in ok.stdout
    # Even if someone bypasses build_argv, the job module refuses out-of-range values.
    bad = subprocess.run([sys.executable, "-m", "greensettle.jobs.burn", "--seconds", "61", "--workers", "1"],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "outside" in bad.stderr
