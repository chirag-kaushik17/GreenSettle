"""Job type `primes`: count the primes below `limit` (sieve of Eratosthenes).

    python -m greensettle.jobs.primes --limit 5000000
"""

from __future__ import annotations

from greensettle.jobs._args import parse_job_args


def count_primes(limit: int) -> int:
    sieve = bytearray([1]) * limit
    sieve[0:2] = b"\x00\x00"
    for n in range(2, int(limit**0.5) + 1):
        if sieve[n]:
            sieve[n * n :: n] = bytes(len(range(n * n, limit, n)))
    return sum(sieve)


def main(argv: list[str] | None = None) -> None:
    p = parse_job_args("primes", argv)
    print(f"primes: {count_primes(p['limit'])} primes below {p['limit']}")


if __name__ == "__main__":
    main()
