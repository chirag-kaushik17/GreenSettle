"""M5: worker and client against a REAL HTTP server (uvicorn in a thread),
with the mock LHM. Hardware-free; uses a fake choose_slot (start now)."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest
import uvicorn

from greensettle import client
from greensettle.agent import meter, worker
from greensettle.agent.lhm_client import LhmClient
from greensettle.server.app import create_app
from greensettle.server.config import ServerConfig
from tests.fakes import start_now_choose
from tests.mock_lhm import MockLhmServer

AGENT, ADMIN = "agent-token-0123456789", "admin-token-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real server on a free port, plus env vars for the client."""
    port = free_port()
    cfg = ServerConfig(agent_token=AGENT, admin_token=ADMIN, db_path=tmp_path / "g.db", time_scale=60,
                       run_tick_thread=False)
    app = create_app(cfg, choose=start_now_choose)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("GREENSETTLE_ADMIN_TOKEN", ADMIN)
    yield url, tmp_path / "g.db"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def lhm():
    mock = MockLhmServer(watts_fn=lambda: 30.0).start()
    yield mock
    mock.stop()


IDLE = meter.IdleCalibration(mean_w=5.0, std_w=0.2, samples=30, seconds=30, measured_at="2026-10-28T00:00:00+00:00")


def make_worker(url: str, lhm: MockLhmServer) -> worker.Worker:
    return worker.Worker(server=url, agent_id="laptop", token=AGENT, read_watts=LhmClient(lhm.url).package_watts,
                         idle=IDLE, meter_config=meter.MeterConfig(poll_interval_s=0.2), sleep=lambda s: None)


def client_main(url: str, *argv: str) -> int:
    return client.main(["--server", url, *argv])


def api_key_for(url: str, user: str, capsys) -> str:
    assert client_main(url, "admin-user", user) == 0
    key = json.loads(capsys.readouterr().out)["api_key"]
    assert client_main(url, "admin-allowance", user, "--amount", "100") == 0
    capsys.readouterr()
    return key


# ---------------------------------------------------------------- worker
def test_worker_refuses_to_start_without_idle_calibration(tmp_path, caplog):
    with pytest.raises(worker.WorkerError, match="calibrate-idle"):
        worker.load_idle(tmp_path)
    IDLE.save(tmp_path / "idle.json")
    with caplog.at_level("WARNING"):
        worker.load_idle(tmp_path, now=datetime(2026, 11, 5, tzinfo=timezone.utc))  # 5 days old
    assert "old" in caplog.text


def test_worker_main_needs_the_token(monkeypatch, capsys):
    monkeypatch.delenv("GREENSETTLE_AGENT_TOKEN", raising=False)
    assert worker.main([]) == 2 and "GREENSETTLE_AGENT_TOKEN" in capsys.readouterr().err


def test_client_submit_worker_runs_and_settles(live, lhm, monkeypatch, capsys):
    url, _ = live
    monkeypatch.setenv("GREENSETTLE_API_KEY", api_key_for(url, "alice", capsys))
    assert client_main(url, "submit", "--type", "burn", "--param", "seconds=1", "--param", "workers=1",
                       "--max-delay-h", "2", "--est-duration-s", "2") == 0
    job_id = capsys.readouterr().out.split()[1].rstrip(":")

    w = make_worker(url, lhm)
    assert w.process_one() is True  # claim -> run (real subprocess) -> post result
    assert w.process_one() is False  # nothing left

    assert client_main(url, "status", job_id) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "done" and status["settlement"]["fee"] > 0
    assert status["settlement"]["zone"] in ("saving", "dead_zone", "backfire")
    assert client_main(url, "me") == 0
    me = json.loads(capsys.readouterr().out)
    assert me["balance"] < 100 and "fee" in [e["kind"] for e in me["last_entries"]]


def test_worker_revalidates_and_reports_a_bad_job_as_failed(live, lhm, monkeypatch, capsys):
    url, db_path = live
    monkeypatch.setenv("GREENSETTLE_API_KEY", api_key_for(url, "alice", capsys))
    client_main(url, "submit", "--type", "burn", "--param", "seconds=1", "--param", "workers=1",
                "--max-delay-h", "0", "--est-duration-s", "2")
    job_id = capsys.readouterr().out.split()[1].rstrip(":")
    # Simulate a compromised server/database handing out an out-of-range job.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE jobs SET params_json = ? WHERE job_id = ?", (json.dumps({"seconds": 600, "workers": 1}), job_id))
    conn.commit()
    conn.close()
    w = make_worker(url, lhm)
    assert w.process_one() is True
    client_main(url, "status", job_id)
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "failed" and status["settlement"] is None


def test_worker_survives_an_unreachable_server(lhm):
    w = make_worker(f"http://127.0.0.1:{free_port()}", lhm)  # nothing listens there
    with pytest.raises(worker.NetworkError):
        w.claim()
    assert w.post_result({"job_id": "j"}) is None  # retried, logged, not raised
    stopper = threading.Timer(0.3, w.stop)
    stopper.start()
    w.slot_loop()  # returns once stopped, without crashing


def test_telemetry_is_posted_in_batches(live, lhm, monkeypatch, capsys):
    url, _ = live
    monkeypatch.setattr(worker, "TELEMETRY_SAMPLE_S", 0.05)
    monkeypatch.setattr(worker, "TELEMETRY_BATCH_S", 0.2)
    monkeypatch.setenv("GREENSETTLE_API_KEY", api_key_for(url, "alice", capsys))
    w = make_worker(url, lhm)
    t = threading.Thread(target=w.telemetry_loop, daemon=True)
    t.start()
    time.sleep(0.8)
    w.stop()
    t.join(timeout=5)
    from greensettle.httpjson import request_json
    rows = request_json("GET", f"{url}/telemetry/latest?n=60",
                        headers={"X-API-Key": client.os.environ["GREENSETTLE_API_KEY"]}).data
    assert len(rows) >= 2 and all(r["watts"] == 30.0 and r["gco2e_per_s"] > 0 for r in rows)


# ---------------------------------------------------------------- client
def test_client_param_parsing_and_errors(live, monkeypatch, capsys):
    url, _ = live
    with pytest.raises(SystemExit):
        client.build_parser().parse_args(["submit", "--type", "burn", "--param", "seconds=ten",
                                          "--max-delay-h", "1", "--est-duration-s", "2"])
    monkeypatch.delenv("GREENSETTLE_API_KEY", raising=False)
    assert client_main(url, "me") == 2 and "GREENSETTLE_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("GREENSETTLE_API_KEY", "wrong")
    assert client_main(url, "me") == 2 and "HTTP 401" in capsys.readouterr().err
    assert client.main(["--server", f"http://127.0.0.1:{free_port()}", "me"]) == 2  # unreachable


def test_client_admin_intensity_and_cancel(live, monkeypatch, capsys):
    url, _ = live
    monkeypatch.setenv("GREENSETTLE_API_KEY", api_key_for(url, "alice", capsys))
    assert client_main(url, "admin-intensity", "dirty") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "dirty"
    client_main(url, "submit", "--type", "primes", "--param", "limit=1000", "--max-delay-h", "1",
                "--est-duration-s", "1")
    job_id = capsys.readouterr().out.split()[1].rstrip(":")
    assert client_main(url, "cancel", job_id) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert client_main(url, "jobs") == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "cancelled"
    assert client_main(url, "admin-state") == 0
    assert "users" in json.loads(capsys.readouterr().out)
