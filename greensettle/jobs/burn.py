"""Job type `burn`: keep N cores busy for S seconds (catalog caps S at 60).

    python -m greensettle.jobs.burn --seconds 20 --workers 2

Workers are processes, not threads (Python's GIL would put threads on one core).
"""

from __future__ import annotations

import multiprocessing as mp
import time

from greensettle.jobs._args import parse_job_args


def busy_loop(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    x = 0
    while time.monotonic() < deadline:
        for i in range(10_000):
            x = (x * 31 + i) % 1_000_003


def main(argv: list[str] | None = None) -> None:
    p = parse_job_args("burn", argv)
    procs = [mp.Process(target=busy_loop, args=(p["seconds"],)) for _ in range(p["workers"])]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    print(f"burn: {p['workers']} worker(s) x {p['seconds']} s done")


if __name__ == "__main__":  # required on Windows (multiprocessing re-imports this module)
    main()
