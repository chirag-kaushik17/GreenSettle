"""The fixed catalog of job types.

SECURITY RULE: a job is NEVER a command. A job is {job_type, params}, where
job_type must be one of the names below and every param is a whole number
inside a hard range. The worker re-validates the job itself (it does not
trust the server) and builds the argv list from this catalog:

    [sys.executable, "-m", "greensettle.jobs.<type>", "--<param>", "<int>", ...]

No shell is used, and no user-supplied path or string ever reaches a
process: only the type name (checked against this catalog) and integers.

The ranges are PLACEHOLDERS chosen so that every job finishes in about a
minute or less. burn is capped at 60 s because this laptop reached 103 C
under load.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

MAX_EST_DURATION_S = 120.0  # users may not declare a longer estimated duration


@dataclass(frozen=True)
class IntParam:
    min: int
    max: int
    help: str


@dataclass(frozen=True)
class JobType:
    name: str
    params: dict[str, IntParam]
    description: str

    @property
    def module(self) -> str:
        return f"greensettle.jobs.{self.name}"


def _cpu_count() -> int:
    return os.cpu_count() or 1


def catalog(cpu_count: int | None = None) -> dict[str, JobType]:
    """Build the catalog. `workers` is limited by the CPU count of the machine
    doing the validating (so the worker's own check uses the laptop's count)."""
    cpus = cpu_count if cpu_count is not None else _cpu_count()
    return {
        "burn": JobType("burn", {
            "seconds": IntParam(1, 60, "how long to keep the CPUs busy (hard cap: 60 s, thermal safety)"),
            "workers": IntParam(1, cpus, "number of busy worker processes"),
        }, "busy-loop on N cores for S seconds"),
        "hash": JobType("hash", {
            "megabytes": IntParam(1, 64, "size of the data block to hash"),
            "repeats": IntParam(1, 400, "how many times to hash it"),
        }, "SHA-256 over a deterministic data block"),
        "primes": JobType("primes", {
            "limit": IntParam(2, 100_000_000, "count primes below this number"),
        }, "sieve of Eratosthenes"),
    }


class CatalogError(ValueError):
    """The job is not in the catalog or a parameter is not allowed."""


def validate(job_type: Any, params: Any, cpu_count: int | None = None) -> dict[str, int]:
    """Return a clean {name: int} dict, or raise CatalogError.

    Rejects: unknown job types, non-dict params, unknown or missing params,
    anything that is not a plain int (bools, floats and strings included),
    and values outside the range.
    """
    types = catalog(cpu_count)
    if not isinstance(job_type, str) or job_type not in types:
        raise CatalogError(f"unknown job_type {job_type!r}; allowed: {sorted(types)}")
    spec = types[job_type]
    if not isinstance(params, Mapping):
        raise CatalogError("params must be an object")
    unknown = set(params) - set(spec.params)
    if unknown:
        raise CatalogError(f"unknown parameter(s) for {job_type}: {sorted(map(str, unknown))}")
    missing = set(spec.params) - set(params)
    if missing:
        raise CatalogError(f"missing parameter(s) for {job_type}: {sorted(missing)}")
    clean: dict[str, int] = {}
    for name, rule in spec.params.items():
        value = params[name]
        if type(value) is not int:  # rejects bool (a subclass of int), float, str, ...
            raise CatalogError(f"{job_type}.{name} must be an integer, got {type(value).__name__}")
        if not rule.min <= value <= rule.max:
            raise CatalogError(f"{job_type}.{name}={value} is outside [{rule.min}, {rule.max}]")
        clean[name] = value
    return clean


def build_argv(job_type: str, params: Mapping[str, Any], cpu_count: int | None = None) -> list[str]:
    """Validate again and build the exact argv list. Only this function decides argv."""
    clean = validate(job_type, params, cpu_count)
    argv = [sys.executable, "-m", catalog(cpu_count)[job_type].module]
    for name in sorted(clean):
        argv += [f"--{name}", str(clean[name])]
    return argv


def validate_est_duration(est_duration_s: Any) -> float:
    if isinstance(est_duration_s, bool) or not isinstance(est_duration_s, (int, float)):
        raise CatalogError("est_duration_s must be a number")
    value = float(est_duration_s)
    if not 0 < value <= MAX_EST_DURATION_S:
        raise CatalogError(f"est_duration_s must be in (0, {MAX_EST_DURATION_S:g}]")
    return value
