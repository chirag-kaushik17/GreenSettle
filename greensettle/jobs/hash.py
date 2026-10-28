"""Job type `hash`: SHA-256 over a deterministic block of data, repeated.

    python -m greensettle.jobs.hash --megabytes 32 --repeats 5
"""

from __future__ import annotations

import hashlib

from greensettle.jobs._args import parse_job_args


def run(megabytes: int, repeats: int) -> str:
    block = bytes(range(256)) * (megabytes * 4096)  # 256 B * 4096 = 1 MiB per megabyte
    digest = hashlib.sha256()
    for _ in range(repeats):
        digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> None:
    p = parse_job_args("hash", argv)
    print(f"hash: sha256 = {run(p['megabytes'], p['repeats'])}")


if __name__ == "__main__":
    main()
