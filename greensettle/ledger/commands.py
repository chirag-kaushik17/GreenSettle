"""Phase 2 CLI commands (offline: work directly on the SQLite file, no server).

    python -m greensettle.cli user-add alice
    python -m greensettle.cli allowance alice [--period 2026-11] [--amount 100]
    python -m greensettle.cli settle data/measurements/<job>.json --user alice \\
        --submit-time 2026-11-02T08:00:00+00:00 --run-time 2026-11-02T13:00:00+00:00
    python -m greensettle.cli balance alice
    python -m greensettle.cli ledger alice [--limit 20]

Global options (before the command): --db PATH, --clock ISO.
The simulated "now" for demos comes from --clock, else from the environment
variable GREENSETTLE_CLOCK, else from the real clock. The CLI is the only
place that reads the real clock; everything below it receives `now`.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from greensettle.intensity.provider import SimulatedCurve
from greensettle.intensity.timeline import mean_intensity
from greensettle.ledger import db
from greensettle.ledger.config import DEFAULT_CONFIG, tier_for
from greensettle.ledger.credits import MICRO_PER_CREDIT
from greensettle.ledger.settle import record_offline_job, settle_job


def parse_time(text: str) -> datetime:
    """ISO-8601 with a time zone, e.g. 2026-11-02T13:00:00+00:00 or ...Z."""
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError(f"time {text!r} has no time zone; add +00:00 (UTC) or e.g. +05:30")
    return t


CLOCK_ENV_VAR = "GREENSETTLE_CLOCK"


def now_of(args: argparse.Namespace) -> datetime:
    """--clock ISO, else $GREENSETTLE_CLOCK, else the real clock (UTC)."""
    text = args.clock or os.environ.get(CLOCK_ENV_VAR)
    return parse_time(text) if text else datetime.now(timezone.utc)


def credits(micro: int) -> str:
    return f"{micro / MICRO_PER_CREDIT:,.6f}"


# --------------------------------------------------------------------------
def cmd_user_add(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.create_user(conn, args.name, now_of(args))
    print(f"created user {args.name} (balance 0, tier {tier_for(0).name})")
    return 0


def cmd_allowance(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    res = db.grant_allowance(conn, args.user, now_of(args), args.amount, args.period)
    print(res.message)
    if res.decay_micro:
        print(f"  decay applied first: -{credits(res.decay_micro)}")
    print(f"  balance: {credits(db.balance_micro(conn, args.user))} credits "
          f"(tier {db.tier_of_user(conn, args.user).name})")
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    """Settle one Phase 1 measurement file offline.

    i_run and i_submit both come from the simulated curve (Phase 1's
    SimulatedCurve, NOT forced: a forced state would give both times the
    same value). Each is the mean over the job's duration starting at the
    run time and at the submit time respectively.
    """
    m = json.loads(Path(args.measurement).read_text(encoding="utf-8"))
    job_id, duration = m["job_id"], timedelta(seconds=float(m["duration_s"]))
    energy, energy_unc = float(m["energy_kwh"]["job"]), float(m["uncertainty_kwh"]["combined"])
    submit_t, run_t = parse_time(args.submit_time), parse_time(args.run_time)

    curve = SimulatedCurve()
    i_submit, i_submit_unc = mean_intensity(curve, submit_t, submit_t + duration)
    i_run, i_run_unc = mean_intensity(curve, run_t, run_t + duration)

    conn = db.connect(args.db)
    record_offline_job(conn, job_id=job_id, user_id=args.user, submitted_at=submit_t,
                       i_submit=i_submit, i_submit_unc=i_submit_unc, source="simulated curve (offline settle)")
    out = settle_job(conn, job_id=job_id, user_id=args.user, now=now_of(args),
                     energy_kwh=energy, energy_unc_kwh=energy_unc,
                     i_submit=i_submit, i_submit_unc=i_submit_unc, i_run=i_run, i_run_unc=i_run_unc)
    if out.already_settled:
        print(f"{job_id} was already settled; showing the stored result (nothing written).")
    print(f"job        {job_id}")
    print(f"energy     {energy:.6g} ± {energy_unc:.2g} kWh")
    print(f"i_submit   {i_submit:.1f} ± {i_submit_unc:.1f} g/kWh   ({submit_t.isoformat()})")
    print(f"i_run      {i_run:.1f} ± {i_run_unc:.1f} g/kWh   ({run_t.isoformat()})")
    print(f"saved      {out.saved_g:.6f} ± {out.saved_unc_g:.6f} g CO2e")
    print(f"zone       {out.zone}")
    print(f"fee        {credits(out.fee_micro)}")
    print(f"rebate     {credits(out.rebate_micro)}")
    print(f"surcharge  {credits(out.surcharge_micro)}")
    print(f"net        {credits(out.net_micro)}")
    print(f"balance    {credits(out.balance_micro_after)} credits (tier {out.tier_after})")
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    bal = db.balance_micro(conn, args.user)
    last = db.last_decay_at(conn, args.user)
    now = now_of(args)
    pending = db.decay_amount_micro(bal, (now - last).total_seconds() / 86_400, DEFAULT_CONFIG.half_life_days)
    print(f"user        {args.user}")
    print(f"balance     {credits(bal)} credits")
    print(f"tier        {tier_for(bal / MICRO_PER_CREDIT).name}")
    print(f"last decay  {last.isoformat()}")
    print(f"pending     -{credits(pending)} decay, applied at the next allowance or settlement")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    if not db.user_exists(conn, args.user):
        raise ValueError(f"unknown user {args.user!r}")
    rows = conn.execute(
        "SELECT * FROM ledger_with_balance WHERE user_id = ? ORDER BY entry_id DESC LIMIT ?",  # newest first
        (args.user, args.limit),
    ).fetchall()
    print(f"{'id':>5}  {'time (UTC)':25}  {'kind':10}  {'amount':>16}  {'balance':>16}  job / period")
    for r in rows:
        ref = r["job_id"] or r["period_key"] or ""
        print(f"{r['entry_id']:>5}  {r['ts'][:25]:25}  {r['kind']:10}  {credits(r['amount_micro']):>16}  "
              f"{credits(r['balance_micro']):>16}  {ref}")
    return 0


# --------------------------------------------------------------------------
def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("user-add", help="create a ledger user")
    p.add_argument("name")
    p.set_defaults(func=cmd_user_add)

    p = sub.add_parser("allowance", help="grant the monthly allowance (idempotent per period)")
    p.add_argument("user")
    p.add_argument("--period", help="YYYY-MM (default: the month of --now)")
    p.add_argument("--amount", type=float, help=f"credits (default {DEFAULT_CONFIG.allowance_credits:g})")
    p.set_defaults(func=cmd_allowance)

    p = sub.add_parser("settle", help="settle a Phase 1 measurement file")
    p.add_argument("measurement", help="data/measurements/<job_id>.json")
    p.add_argument("--user", required=True)
    p.add_argument("--submit-time", required=True, help="ISO time the job was submitted (counterfactual start)")
    p.add_argument("--run-time", required=True, help="ISO time the job actually ran")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("balance", help="balance, tier and last decay time")
    p.add_argument("user")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("ledger", help="a user's ledger rows, newest first, with running balance")
    p.add_argument("user")
    p.add_argument("--limit", type=int, default=20, help="how many rows (default 20)")
    p.set_defaults(func=cmd_ledger)
