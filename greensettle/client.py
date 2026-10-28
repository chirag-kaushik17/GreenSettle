"""Tiny command-line client for the GreenSettle server (stdlib urllib only).

User commands (API key from $GREENSETTLE_API_KEY):
    python -m greensettle.client submit --type burn --param seconds=20 --param workers=2 \\
        --max-delay-h 6 --est-duration-s 25
    python -m greensettle.client status JOB_ID
    python -m greensettle.client jobs
    python -m greensettle.client me
    python -m greensettle.client cancel JOB_ID

Admin commands (token from $GREENSETTLE_ADMIN_TOKEN):
    python -m greensettle.client admin-user alice          # prints alice's API key ONCE
    python -m greensettle.client admin-allowance alice [--amount 100] [--period 2026-11]
    python -m greensettle.client admin-intensity dirty      # green | dirty | off, from the server's sim "now"
    python -m greensettle.client admin-state

Server address: --server (default http://127.0.0.1:8000).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from greensettle.httpjson import NetworkError, request_json

API_KEY_ENV = "GREENSETTLE_API_KEY"
ADMIN_TOKEN_ENV = "GREENSETTLE_ADMIN_TOKEN"


class ClientError(RuntimeError):
    pass


def parse_param(text: str) -> tuple[str, int]:
    """'seconds=20' -> ('seconds', 20). Values must be whole numbers."""
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"--param must look like name=value, not {text!r}")
    try:
        return name, int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--param {name}: {value!r} is not a whole number") from None


def env_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ClientError(f"set the environment variable {name}")
    return value


def call(args: argparse.Namespace, method: str, path: str, body: Any = None, admin: bool = False) -> Any:
    headers = ({"X-Admin-Token": env_secret(ADMIN_TOKEN_ENV)} if admin
               else {"X-API-Key": env_secret(API_KEY_ENV)})
    reply = request_json(method, args.server.rstrip("/") + path, body, headers)
    if reply.status >= 400:
        raise ClientError(f"HTTP {reply.status}: {json.dumps(reply.data)}")
    return reply.data


def show(data: Any) -> None:
    print(json.dumps(data, indent=2))


def cmd_submit(args: argparse.Namespace) -> None:
    body = {"job_type": args.type, "params": dict(args.param), "est_duration_s": args.est_duration_s,
            "max_delay_h": args.max_delay_h}
    plan = call(args, "POST", "/jobs", body)
    print(f"job {plan['job_id']}: {'DEFERRED' if plan['deferred'] else 'starts now'} ({plan['reason']})")
    print(f"  planned start   {plan['planned_start']}   (latest {plan['latest_start']})")
    print(f"  forecast        {plan['forecast_mean']:.1f} ± {plan['forecast_unc']:.1f} g/kWh "
          f"(now: {plan['forecast_now_mean']:.1f}, expected saving {plan['expected_saving']:.1f})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m greensettle.client", description="GreenSettle client")
    parser.add_argument("--server", default=os.environ.get("GREENSETTLE_SERVER", "http://127.0.0.1:8000"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="submit a catalog job")
    p.add_argument("--type", required=True, help="burn | hash | primes")
    p.add_argument("--param", type=parse_param, action="append", default=[], help="name=integer (repeatable)")
    p.add_argument("--max-delay-h", type=float, required=True, help="how long you can wait BEFORE it starts")
    p.add_argument("--est-duration-s", type=float, required=True, help="estimated run time in real seconds")
    p.set_defaults(func=cmd_submit)

    for name, helptext in (("status", "one job (yours only)"), ("cancel", "cancel a queued job")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("job_id")
    sub.choices["status"].set_defaults(func=lambda a: show(call(a, "GET", f"/jobs/{a.job_id}")))
    sub.choices["cancel"].set_defaults(func=lambda a: show(call(a, "DELETE", f"/jobs/{a.job_id}")))
    sub.add_parser("jobs", help="your recent jobs").set_defaults(func=lambda a: show(call(a, "GET", "/jobs")))
    sub.add_parser("me", help="balance, tier, last ledger rows").set_defaults(
        func=lambda a: show(call(a, "GET", "/me")))

    p = sub.add_parser("admin-user", help="create a user and print their API key once")
    p.add_argument("user_id")
    p.set_defaults(func=lambda a: show(call(a, "POST", "/admin/users", {"user_id": a.user_id}, admin=True)))
    p = sub.add_parser("admin-allowance", help="grant a monthly allowance")
    p.add_argument("user_id")
    p.add_argument("--amount", type=float)
    p.add_argument("--period")
    p.set_defaults(func=lambda a: show(call(a, "POST", "/admin/allowance", {
        k: v for k, v in (("user_id", a.user_id), ("amount", a.amount), ("period", a.period)) if v is not None
    }, admin=True)))
    p = sub.add_parser("admin-intensity", help="demo override from the server's current sim time")
    p.add_argument("state", choices=["green", "dirty", "off"])
    p.set_defaults(func=lambda a: show(call(a, "POST", "/admin/intensity", {"state": a.state}, admin=True)))
    sub.add_parser("admin-state", help="queue, running jobs, settlements, balances").set_defaults(
        func=lambda a: show(call(a, "GET", "/admin/state", admin=True)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ClientError, NetworkError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
