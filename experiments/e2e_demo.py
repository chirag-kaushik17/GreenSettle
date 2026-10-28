"""End-to-end demo: server + worker in one process, narrated.

    python experiments/e2e_demo.py --mock-lhm --time-scale 600

What happens (simulated time starts at 08:00 UTC, when the simulated grid
is still fairly dirty; the solar dip is around 13:00):
  1. alice (green tier) and bob (standard tier) get allowances.
  2. alice submits job A (3 h window) and job B (6 h window); the plans
     returned by choose_slot are shown.
  3. Job A runs at its planned slot and settles; the rebate and new balance
     are printed.
  4. Shortly before job B's slot the admin flips the grid to "dirty". The
     forecast did not know, so B runs on a dirty grid and settles in the
     "backfire" zone with a higher fee.
  5. alice's balance has crossed a tier threshold, and the dispatch order
     for two equal jobs from alice and bob changes.

Deterministic: fixed sim start, fixed curve and forecast seeds, fresh
temporary database. Only the real run times of the jobs vary slightly.
"""

from __future__ import annotations

import argparse
import secrets
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow `python experiments/e2e_demo.py` from the project root

import uvicorn  # noqa: E402

from greensettle.agent import lhm_client, meter, worker  # noqa: E402
from greensettle.httpjson import request_json  # noqa: E402
from greensettle.ledger.config import DEFAULT_TIERS  # noqa: E402
from greensettle.scheduler.dispatch import QueuedJob, dispatch_order  # noqa: E402
from greensettle.server.app import create_app  # noqa: E402
from greensettle.server.config import ServerConfig  # noqa: E402

SIM_START = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
JOB = {"job_type": "burn", "params": {"seconds": 5, "workers": 2}, "est_duration_s": 6}
FLIP_BEFORE = timedelta(minutes=15)  # sim time before job B's slot when the grid turns dirty


def say(text: str = "") -> None:
    print(text, flush=True)


def heading(text: str) -> None:
    say()
    say(f"=== {text} " + "=" * max(0, 66 - len(text)))


class Demo:
    def __init__(self, scale: float, mock_lhm: bool, lhm_url: str | None,
                 choose=None, job: dict | None = None) -> None:
        """choose/job: test hooks only (a stand-in slot rule and a shorter job).
        The real demo always uses the student's choose_slot."""
        self.scale = scale
        self.choose = choose
        self.job = job or JOB
        self.tmp = tempfile.TemporaryDirectory(prefix="greensettle-demo-")
        self.agent_token, self.admin_token = secrets.token_hex(32), secrets.token_hex(32)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.mock_lhm = mock_lhm
        self.lhm_url = lhm_url
        self.keys: dict[str, str] = {}

    # ---- HTTP helpers ---------------------------------------------------------
    def admin(self, method: str, path: str, body=None):
        return request_json(method, self.url + path, body, {"X-Admin-Token": self.admin_token})

    def as_user(self, user: str, method: str, path: str, body=None):
        return request_json(method, self.url + path, body, {"X-API-Key": self.keys[user]})

    def state(self) -> dict:
        return self.admin("GET", "/admin/state").data

    def sim_now(self) -> datetime:
        return datetime.fromisoformat(self.state()["sim_now"])

    def wait_until_sim(self, target: datetime, why: str) -> None:
        real_s = max(0.0, (target - self.sim_now()).total_seconds() / self.scale)
        say(f"   ... waiting {real_s:.0f} real s for sim time {target:%H:%M} ({why})")
        time.sleep(real_s)

    def wait_for_settlement(self, user: str, job_id: str, timeout_s: float = 300) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            job = self.as_user(user, "GET", f"/jobs/{job_id}").data
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.5)
        raise TimeoutError(f"{job_id} did not finish in {timeout_s} s")

    # ---- start / stop -----------------------------------------------------------
    def start(self) -> None:
        cfg = ServerConfig(agent_token=self.agent_token, admin_token=self.admin_token,
                           db_path=Path(self.tmp.name) / "demo.db", time_scale=self.scale, sim_start=SIM_START)
        app = create_app(cfg) if self.choose is None else create_app(cfg, choose=self.choose)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.05)

        if self.mock_lhm:
            from tests.mock_lhm import MockLhmServer, follow_load_watts
            self.mock = MockLhmServer(watts_fn=follow_load_watts()).start()
            lhm = lhm_client.LhmClient(self.mock.url)
            idle = meter.IdleCalibration(4.5, 0.3, 30, 30.0, SIM_START.isoformat())  # the mock's idle level
            say("Using the MOCK LibreHardwareMonitor (watts follow CPU load, idle 4.5 W). Not real power.")
        else:
            self.mock = None
            lhm = lhm_client.LhmClient(lhm_client.resolve_url(self.lhm_url))
            idle = worker.load_idle(ROOT / "data")
            say(f"Using the real LibreHardwareMonitor at {lhm.url}; idle {idle.mean_w:.2f} W.")
        self.worker = worker.Worker(server=self.url, agent_id="demo-laptop", token=self.agent_token,
                                    read_watts=lhm.package_watts, idle=idle, backoff_max_s=1.0)
        for target in (self.worker.slot_loop, self.worker.telemetry_loop):
            threading.Thread(target=target, daemon=True).start()
        say(f"Server on {self.url} (time scale x{self.scale:g}: 1 real second = {self.scale / 60:g} sim minutes).")

    def stop(self) -> None:
        self.worker.stop()
        self.server.should_exit = True
        if self.mock:
            self.mock.stop()
        time.sleep(0.5)
        self.tmp.cleanup()

    # ---- the story ----------------------------------------------------------------
    def users(self) -> None:
        heading("1. Users and allowances")
        for user, credits in (("alice", 100.0), ("bob", 60.0)):
            self.keys[user] = self.admin("POST", "/admin/users", {"user_id": user}).data["api_key"]
            self.admin("POST", "/admin/allowance", {"user_id": user, "amount": credits})
        self.show_balances()

    def show_balances(self) -> dict[str, dict]:
        users = {u["user_id"]: u for u in self.state()["users"]}
        for u in users.values():
            say(f"   {u['user_id']:6} balance {u['balance']:9.4f} credits   tier {u['tier']}")
        return users

    def submit(self, user: str, max_delay_h: float, label: str) -> dict | None:
        reply = self.as_user(user, "POST", "/jobs", self.job | {"max_delay_h": max_delay_h})
        if reply.status == 501:
            say(f"\n   STOP: the server answered 501: {reply.data['detail']}")
            say("   The server could not plan the job, so the demo stops here.")
            return None
        if reply.status != 201:
            raise RuntimeError(f"submit failed: HTTP {reply.status} {reply.data}")
        p = reply.data
        say(f"   job {label} ({p['job_id']}), window {max_delay_h:g} h:")
        say(f"      choose_slot -> {p['reason']}; planned start {p['planned_start'][11:16]} "
            f"(latest {p['latest_start'][11:16]})")
        say(f"      forecast now {p['forecast_now_mean']:.0f} g/kWh, chosen slot {p['forecast_mean']:.0f} "
            f"± {p['forecast_unc']:.0f}, expected saving {p['expected_saving']:.0f} g/kWh")
        return p

    def show_settlement(self, job: dict) -> None:
        s = job["settlement"]
        if s is None:
            say(f"   job {job['job_id']} {job['status']}: not settled ({job.get('failure_reason')})")
            return
        say(f"   zone {s['zone'].upper()}: i_submit {s['i_submit']:.0f} vs i_run {s['i_run']:.0f} g/kWh, "
            f"energy {s['energy_kwh'] * 1000:.4f} Wh")
        say(f"   fee {s['fee']:.4f}  rebate {s['rebate']:.4f}  surcharge {s['surcharge']:.4f}  "
            f"-> net cost {s['net']:.4f} credits")

    def run(self) -> int:
        self.users()

        heading("2. Submit at 08:00 sim time (grid still fairly dirty)")
        say(f"   actual intensity now: {self.state()['intensity_now']['g_per_kwh']:.0f} g/kWh")
        tiers_before = {u["user_id"]: u["tier"] for u in self.state()["users"]}
        plan_a = self.submit("alice", 3.0, "A")
        if plan_a is None:
            return 3
        plan_b = self.submit("alice", 6.0, "B")
        start_a, start_b = (datetime.fromisoformat(p["planned_start"]) for p in (plan_a, plan_b))

        heading("3. Job A runs at its planned slot and settles")
        self.wait_until_sim(start_a, "job A's slot")
        job_a = self.wait_for_settlement("alice", plan_a["job_id"])
        self.show_settlement(job_a)
        self.show_balances()

        heading("4. The grid turns dirty just before job B's slot -> backfire")
        if start_b <= self.sim_now() + timedelta(minutes=5):
            say("   NOTE: job B's slot is not later than now, so it may already be running; "
                "the flip below can only affect what is left of its run.")
        else:
            self.wait_until_sim(start_b - FLIP_BEFORE, "15 sim minutes before job B")
        flip = self.admin("POST", "/admin/intensity", {"state": "dirty"}).data
        say(f"   admin: intensity override 'dirty' from {flip['from_sim'][11:16]} (the past is unchanged)")
        job_b = self.wait_for_settlement("alice", plan_b["job_id"])
        self.show_settlement(job_b)
        users = self.show_balances()

        heading("5. Tiers and dispatch order")
        tiers_after = {u: users[u]["tier"] for u in users}
        for u in users:
            change = "unchanged" if tiers_before[u] == tiers_after[u] else f"{tiers_before[u]} -> {tiers_after[u]}"
            say(f"   {u}: {change}")
        now = self.sim_now()
        # Two equal, ready jobs: bob's was submitted first. Which one would the dispatcher pick first?
        jobs = [QueuedJob("bob-job", "bob", now, now + timedelta(hours=6), now - timedelta(minutes=1), 1),
                QueuedJob("alice-job", "alice", now, now + timedelta(hours=6), now, 2)]
        for label, tiers in (("at the start", tiers_before), ("now", tiers_after)):
            order = dispatch_order(jobs, now, lambda u, t=tiers: _tier_named(t[u]))
            say(f"   dispatch order {label:12}: {' -> '.join(j.job_id for j in order)}   "
                f"(alice {tiers['alice']}, bob {tiers['bob']})")
        say()
        say("Demo finished.")
        return 0


def _tier_named(name: str):
    return next(t for t in DEFAULT_TIERS if t.name == name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mock-lhm", action="store_true", help="use the fake LHM (no hardware needed)")
    parser.add_argument("--lhm-url", default=None)
    parser.add_argument("--time-scale", type=float, default=600.0)
    args = parser.parse_args()
    demo = Demo(args.time_scale, args.mock_lhm, args.lhm_url)
    demo.start()
    try:
        return demo.run()
    finally:
        demo.stop()


if __name__ == "__main__":
    sys.exit(main())
