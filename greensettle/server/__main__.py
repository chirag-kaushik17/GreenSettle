"""Run the GreenSettle server.

    $env:GREENSETTLE_AGENT_TOKEN = "<random hex>"   # PowerShell
    $env:GREENSETTLE_ADMIN_TOKEN = "<another random hex>"
    python -m greensettle.server --time-scale 600

Binds to 127.0.0.1 (this machine only) unless --host is given explicitly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from greensettle.server.config import ConfigError, ServerConfig, tokens_from_env

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m greensettle.server", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/greensettle.db")
    parser.add_argument("--time-scale", type=float, default=1.0, help="sim seconds per real second")
    parser.add_argument("--sim-start", default=None, help="ISO time where sim time starts (first start only)")
    parser.add_argument("--max-concurrent-jobs", type=int, default=2)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        agent, admin = tokens_from_env()
        sim_start = datetime.fromisoformat(args.sim_start) if args.sim_start else None
        if sim_start is not None and sim_start.tzinfo is None:
            raise ConfigError("--sim-start needs a time zone, e.g. 2026-11-02T06:00:00+00:00")
        config = ServerConfig(agent_token=agent, admin_token=admin, db_path=args.db, time_scale=args.time_scale,
                              sim_start=sim_start, max_concurrent_jobs=args.max_concurrent_jobs)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.host not in LOOPBACK:
        print(f"WARNING: binding to {args.host}, so other machines can reach this server. The API uses plain "
              "HTTP: put it behind an HTTPS reverse proxy or reach it through an SSH tunnel, never expose it "
              "directly. See deploy/README.md.", file=sys.stderr)

    import uvicorn  # imported late so --help works without it

    from greensettle.server.app import create_app

    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
