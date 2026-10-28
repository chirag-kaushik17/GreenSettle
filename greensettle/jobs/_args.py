"""Shared argument parsing for the job modules: every job re-checks its own
arguments against the catalog, even though the worker already did."""

from __future__ import annotations

import argparse

from greensettle.jobs.catalog import catalog, validate


def parse_job_args(job_type: str, argv: list[str] | None = None) -> dict[str, int]:
    spec = catalog()[job_type]
    parser = argparse.ArgumentParser(prog=f"greensettle.jobs.{job_type}", description=spec.description)
    for name, rule in spec.params.items():
        parser.add_argument(f"--{name}", type=int, required=True, help=f"{rule.help} [{rule.min}..{rule.max}]")
    args = vars(parser.parse_args(argv))
    try:
        return validate(job_type, args)
    except ValueError as exc:
        parser.error(str(exc))
        raise  # unreachable: parser.error exits
