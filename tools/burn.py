"""CPU burner for testing the meter.

    python tools/burn.py --seconds 30 --threads 4

"--threads" is the number of busy WORKERS. Each worker is a separate process
(multiprocessing), not a Python thread: because of Python's GIL, threads
would all share one core and could not load four cores. Using processes also
exercises the meter's child-process CPU tracking.

Safety: this laptop reached 103 C under load, so --seconds is capped at 60.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import time

MAX_SECONDS = 60.0


def busy_loop(seconds: float) -> None:
    """Do pointless arithmetic until the deadline: 100% of one core."""
    deadline = time.monotonic() + seconds
    x = 0
    while time.monotonic() < deadline:
        for i in range(10_000):
            x = (x * 31 + i) % 1_000_003


def main() -> None:
    parser = argparse.ArgumentParser(description="Load N CPU cores for S seconds.")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--threads", type=int, default=1, help="number of busy worker processes")
    args = parser.parse_args()

    seconds = args.seconds
    if seconds > MAX_SECONDS:
        print(f"burn: capping --seconds {seconds:g} to {MAX_SECONDS:g} (thermal safety)")
        seconds = MAX_SECONDS
    workers = max(1, min(args.threads, mp.cpu_count()))

    print(f"burn: {workers} worker process(es) for {seconds:g} s")
    procs = [mp.Process(target=busy_loop, args=(seconds,)) for _ in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print("burn: done")


if __name__ == "__main__":  # required on Windows, where multiprocessing re-imports this file
    main()
