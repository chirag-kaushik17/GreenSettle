"""The worker: runs on the laptop next to LibreHardwareMonitor.

    $env:GREENSETTLE_AGENT_TOKEN = "<same token as the server>"
    python -m greensettle.agent.worker --server http://127.0.0.1:8000 --agent-id laptop

PULL model: the worker asks the server for work; the server never connects
to the worker. For each claimed job it:
  1. re-validates {job_type, params} against ITS OWN catalog check (it does
     not trust the server) and builds a fixed argv list from it,
  2. runs it with the Phase 1 meter (run_job),
  3. posts the measurement to /agent/result.
A separate thread posts CPU Package watts every second, batched every 5 s.
Network errors are logged and retried; they never crash the worker.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from greensettle.agent import lhm_client, meter
from greensettle.httpjson import NetworkError, request_json
from greensettle.jobs import catalog

log = logging.getLogger("greensettle.worker")

AGENT_TOKEN_ENV = "GREENSETTLE_AGENT_TOKEN"
IDLE_MAX_AGE_H = 24.0
BACKOFF_START_S, BACKOFF_MAX_S = 1.0, 10.0
TELEMETRY_SAMPLE_S, TELEMETRY_BATCH_S, TELEMETRY_BUFFER = 1.0, 5.0, 120
RESULT_RETRIES = 5


class WorkerError(RuntimeError):
    """The worker cannot start (missing calibration, token, ...)."""


def load_idle(data_dir: Path, now: datetime | None = None) -> meter.IdleCalibration:
    """Refuse to start without an idle calibration; warn if it is stale."""
    path = data_dir / "idle.json"
    if not path.exists():
        raise WorkerError(f"no idle calibration at {path}: run `python -m greensettle.cli calibrate-idle` first")
    idle = meter.IdleCalibration.load(path)
    age_h = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(idle.measured_at)).total_seconds() / 3600
    if age_h > IDLE_MAX_AGE_H:
        log.warning("idle calibration is %.0f h old (> %g h): consider running calibrate-idle again",
                    age_h, IDLE_MAX_AGE_H)
    return idle


@dataclass
class Worker:
    server: str
    agent_id: str
    token: str
    read_watts: Callable[[], float]
    idle: meter.IdleCalibration
    slots: int = 1
    meter_config: meter.MeterConfig = field(default_factory=meter.MeterConfig)
    backoff_max_s: float = BACKOFF_MAX_S  # longest wait between claims when idle (demos use less)
    sleep: Callable[[float], None] = time.sleep
    _stop: threading.Event = field(default_factory=threading.Event)

    # ---- HTTP -------------------------------------------------------------
    def _post(self, path: str, body: Any):
        return request_json("POST", self.server.rstrip("/") + path, body, {"X-Agent-Token": self.token})

    # ---- one job ------------------------------------------------------------
    def claim(self) -> dict | None:
        reply = self._post("/agent/claim", {"agent_id": self.agent_id})
        if reply.status == 204:
            return None
        if reply.status != 200:
            raise NetworkError(f"claim refused: HTTP {reply.status} {reply.data}")
        return reply.data

    def run(self, job: dict) -> dict:
        """Run one claimed job and return the result body for /agent/result."""
        empty = dict(started_real=time.time(), duration_s=0.0, energy_kwh_job=0.0, energy_unc_kwh=0.0,
                     cpu_share=0.0, job_cpu_s=0.0, system_busy_cpu_s=0.0, samples=0, scope="cpu_package")
        try:
            argv = catalog.build_argv(job["job_type"], job["params"])  # the worker's OWN check
        except (catalog.CatalogError, KeyError, TypeError) as exc:
            log.error("refusing job %s: %s", job.get("job_id"), exc)
            return dict(empty, job_id=job["job_id"], agent_id=self.agent_id, exit_code=2,
                        ended_real=empty["started_real"])
        log.info("running %s: %s %s", job["job_id"], job["job_type"], job["params"])
        started_real = time.time()
        m = meter.run_job(argv, self.read_watts, self.idle, self.meter_config)
        return {
            "job_id": job["job_id"], "agent_id": self.agent_id, "exit_code": m.exit_code,
            "started_real": started_real, "ended_real": started_real + m.duration_s,
            "duration_s": m.duration_s,
            "energy_kwh_job": meter.joules_to_kwh(m.split.job_j),
            "energy_unc_kwh": m.uncertainty.combined_kwh,
            "cpu_share": m.split.cpu_share, "job_cpu_s": m.job_cpu_s,
            "system_busy_cpu_s": max(0.0, m.system_busy_cpu_s), "samples": m.sampler.samples,
            "scope": "cpu_package",
        }

    def post_result(self, result: dict) -> dict | None:
        """Post with retries; a lost result is logged, never raised."""
        delay = BACKOFF_START_S
        for attempt in range(1, RESULT_RETRIES + 1):
            try:
                reply = self._post("/agent/result", result)
            except NetworkError as exc:
                log.warning("result for %s not delivered (attempt %d): %s", result["job_id"], attempt, exc)
            else:
                if reply.status == 200:
                    settlement = (reply.data or {}).get("settlement")
                    log.info("job %s %s; settlement: %s", result["job_id"], reply.data.get("status"),
                             {k: settlement[k] for k in ("zone", "fee", "rebate", "net")} if settlement else None)
                    return reply.data
                log.error("server refused result for %s: HTTP %d %s", result["job_id"], reply.status, reply.data)
                return None  # 4xx: retrying will not help (e.g. the lease was lost)
            self.sleep(delay)
            delay = min(BACKOFF_MAX_S, delay * 2)
        return None

    def process_one(self) -> bool:
        """Claim, run and report one job. Returns False when there was no work."""
        job = self.claim()
        if job is None:
            return False
        self.post_result(self.run(job))
        return True

    # ---- loops ----------------------------------------------------------------
    def slot_loop(self) -> None:
        delay = BACKOFF_START_S
        while not self._stop.is_set():
            try:
                worked = self.process_one()
            except NetworkError as exc:
                log.warning("%s", exc)
                worked = False
            except Exception:  # never let one bad job kill the worker
                log.exception("unexpected error while processing a job")
                worked = False
            if worked:
                delay = BACKOFF_START_S
            else:  # idle or unreachable: back off politely
                self._stop.wait(delay)
                delay = min(self.backoff_max_s, delay * 2)

    def telemetry_loop(self) -> None:
        buffer: list[dict] = []
        last_post = time.monotonic()
        while not self._stop.wait(TELEMETRY_SAMPLE_S):
            try:
                buffer.append({"ts_real": time.time(), "watts": self.read_watts()})
            except Exception as exc:
                log.debug("telemetry read failed: %s", exc)
            buffer = buffer[-TELEMETRY_BUFFER:]  # keep at most 2 minutes if the server is away
            if buffer and time.monotonic() - last_post >= TELEMETRY_BATCH_S:
                try:
                    reply = self._post("/agent/telemetry", {"agent_id": self.agent_id, "samples": buffer})
                    if reply.status == 200:
                        buffer = []
                except NetworkError as exc:
                    log.debug("telemetry not delivered: %s", exc)
                last_post = time.monotonic()

    def run_forever(self) -> None:
        threads = [threading.Thread(target=self.telemetry_loop, name="telemetry", daemon=True)]
        threads += [threading.Thread(target=self.slot_loop, name=f"slot-{i}", daemon=True) for i in range(self.slots)]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("stopping (Ctrl+C)")
            self.stop()

    def stop(self) -> None:
        self._stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m greensettle.agent.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--agent-id", default="laptop")
    parser.add_argument("--lhm-url", default=None, help=f"default: ${lhm_client.URL_ENV_VAR} or {lhm_client.DEFAULT_URL}")
    parser.add_argument("--mock-lhm", action="store_true", help="use the fake LHM server (hardware-free demo)")
    parser.add_argument("--slots", type=int, default=1, help="jobs run at the same time (default 1)")
    parser.add_argument("--data-dir", default="data", help="where idle.json is")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get(AGENT_TOKEN_ENV)
    if not token:
        print(f"ERROR: set {AGENT_TOKEN_ENV} (the same value the server uses)", file=sys.stderr)
        return 2
    try:
        idle = load_idle(Path(args.data_dir))
    except WorkerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    mock = None
    if args.mock_lhm:
        from tests.mock_lhm import MockLhmServer, follow_load_watts

        mock = MockLhmServer(watts_fn=follow_load_watts()).start()
        lhm_url = mock.url
        log.warning("using the MOCK LibreHardwareMonitor at %s (watts follow CPU load; not real power)", lhm_url)
    else:
        lhm_url = lhm_client.resolve_url(args.lhm_url)
    client = lhm_client.LhmClient(lhm_url)
    try:
        client.package_watts()  # fail fast with LHM's helpful message
    except lhm_client.LhmError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    worker = Worker(server=args.server, agent_id=args.agent_id, token=token, read_watts=client.package_watts,
                    idle=idle, slots=max(1, args.slots))
    log.info("worker %s polling %s with %d slot(s)", args.agent_id, args.server, worker.slots)
    worker.run_forever()
    if mock:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
