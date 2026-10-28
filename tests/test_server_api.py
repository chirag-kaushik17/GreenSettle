"""M4: the HTTP API in-process (FastAPI TestClient), with a fake clock and a
fake choose_slot (tests/fakes.py), so everything except the student's slot
rule is checked. The scheduler tick is called directly (no thread)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from greensettle.server.app import create_app, hash_key
from greensettle.server.config import ConfigError, ServerConfig, tokens_from_env
from tests.fakes import FakeRealTime, fake_choose

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
AGENT, ADMIN = "agent-token-0123456789", "admin-token-0123456789"
A_HDR, ADM_HDR = {"X-Agent-Token": AGENT}, {"X-Admin-Token": ADMIN}
SCALE = 600.0  # 6 real seconds = 1 sim hour
BURN = {"job_type": "burn", "params": {"seconds": 2, "workers": 1}, "est_duration_s": 2, "max_delay_h": 4}


class Server:
    def __init__(self, tmp_path, choose=fake_choose, **overrides) -> None:
        self.rt = FakeRealTime(1_000_000.0)
        cfg = dict(agent_token=AGENT, admin_token=ADMIN, db_path=tmp_path / "g.db", time_scale=SCALE,
                   sim_start=T0, run_tick_thread=False)
        self.app = create_app(ServerConfig(**(cfg | overrides)), real_time=self.rt, choose=choose)
        self.client = TestClient(self.app)
        self.state = self.app.state.gs

    def advance_sim(self, delta: timedelta) -> None:
        self.rt.t += delta.total_seconds() / SCALE

    def user(self, name: str, allowance: float | None = 100.0) -> dict[str, str]:
        r = self.client.post("/admin/users", json={"user_id": name}, headers=ADM_HDR)
        assert r.status_code == 201, r.text
        if allowance:
            assert self.client.post("/admin/allowance", json={"user_id": name, "amount": allowance},
                                    headers=ADM_HDR).status_code == 200
        return {"X-API-Key": r.json()["api_key"]}

    def submit(self, key: dict, body: dict = BURN) -> dict:
        r = self.client.post("/jobs", json=body, headers=key)
        assert r.status_code == 201, r.text
        return r.json()

    def claim(self, agent_id: str = "laptop"):
        return self.client.post("/agent/claim", json={"agent_id": agent_id}, headers=A_HDR)

    def result(self, job_id: str, agent_id: str = "laptop", real_s: float = 2.0, **fields):
        start = self.rt.t
        body = dict(job_id=job_id, agent_id=agent_id, exit_code=0, started_real=start, ended_real=start + real_s,
                    duration_s=real_s, energy_kwh_job=0.0005, energy_unc_kwh=0.00005, cpu_share=0.9,
                    job_cpu_s=1.8, system_busy_cpu_s=2.0, samples=3, scope="cpu_package")
        return self.client.post("/agent/result", json=body | fields, headers=A_HDR)


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    with s.client:
        yield s


# ---------------------------------------------------------------- config and tokens
def test_server_refuses_bad_or_missing_tokens(monkeypatch, tmp_path):
    for agent, admin in (("", ADMIN), (AGENT, "short"), (AGENT, AGENT)):
        with pytest.raises(ConfigError):
            ServerConfig(agent_token=agent, admin_token=admin)
    monkeypatch.delenv("GREENSETTLE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("GREENSETTLE_ADMIN_TOKEN", ADMIN)
    with pytest.raises(ConfigError, match="GREENSETTLE_AGENT_TOKEN"):
        tokens_from_env()


def test_server_main_refuses_to_start_without_tokens(monkeypatch, capsys):
    from greensettle.server.__main__ import main
    monkeypatch.delenv("GREENSETTLE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("GREENSETTLE_ADMIN_TOKEN", raising=False)
    assert main([]) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_healthz(srv):
    r = srv.client.get("/healthz")
    assert r.status_code == 200 and r.json()["sim_now"].startswith("2026-11-02T06:00")


# ---------------------------------------------------------------- auth
def test_admin_and_agent_endpoints_need_their_own_token(srv):
    for headers in ({}, {"X-Admin-Token": "wrong-token-xxxxxxxx"}, A_HDR):
        assert srv.client.post("/admin/users", json={"user_id": "x"}, headers=headers).status_code == 401
        assert srv.client.get("/admin/state", headers=headers).status_code == 401
    for headers in ({}, ADM_HDR, {"X-Agent-Token": "wrong-token-xxxxxxxx"}):
        assert srv.client.post("/agent/claim", json={"agent_id": "a"}, headers=headers).status_code == 401


def test_user_endpoints_need_a_valid_key(srv):
    srv.user("alice")
    for headers in ({}, {"X-API-Key": "not-a-key"}, ADM_HDR, A_HDR):
        assert srv.client.get("/me", headers=headers).status_code == 401
        assert srv.client.post("/jobs", json=BURN, headers=headers).status_code == 401


def test_api_key_is_shown_once_and_only_its_hash_is_stored(srv):
    key = srv.user("alice")["X-API-Key"]
    assert len(key) == 64 and int(key, 16) >= 0  # 32 random bytes as hex
    conn = srv.state.connect()
    stored = conn.execute("SELECT api_key_hash FROM users WHERE user_id='alice'").fetchone()[0]
    assert stored == hash_key(key) and key not in stored
    r = srv.client.post("/admin/users", json={"user_id": "alice"}, headers=ADM_HDR)
    assert r.status_code == 409
    assert srv.client.post("/admin/users", json={"user_id": "Bad Name!"}, headers=ADM_HDR).status_code == 422


def test_user_a_cannot_see_or_cancel_user_b_jobs(srv):
    alice, bob = srv.user("alice"), srv.user("bob")
    job = srv.submit(alice)["job_id"]
    assert srv.client.get(f"/jobs/{job}", headers=bob).status_code == 404
    assert srv.client.delete(f"/jobs/{job}", headers=bob).status_code == 404
    assert srv.client.get("/jobs", headers=bob).json() == []
    assert [j["job_id"] for j in srv.client.get("/jobs", headers=alice).json()] == [job]
    assert srv.client.get(f"/jobs/{job}", headers=alice).status_code == 200


# ---------------------------------------------------------------- strict input and limits
@pytest.mark.parametrize("change", [
    {"surprise": 1},                                   # unknown field
    {"est_duration_s": "2"},                           # string, not number
    {"est_duration_s": 121},                           # > 120 s
    {"est_duration_s": 0},
    {"max_delay_h": 49},
    {"max_delay_h": -1},
    {"job_type": "shell"},                             # not in the catalog
    {"params": {"seconds": "2", "workers": 1}},        # catalog: integers only
    {"params": {"seconds": 61, "workers": 1}},         # thermal cap
    {"params": {"seconds": 2, "workers": 1, "cmd": 1}},
])
def test_submit_rejects_bad_requests(srv, change):
    alice = srv.user("alice")
    assert srv.client.post("/jobs", json=BURN | change, headers=alice).status_code == 422


def test_body_size_limit(srv):
    alice = srv.user("alice")
    big = {"job_type": "burn", "params": {f"p{i}": i for i in range(3000)}, "est_duration_s": 2, "max_delay_h": 1}
    assert srv.client.post("/jobs", json=big, headers=alice).status_code == 413

    def chunks():  # no Content-Length header: the streaming check must catch it
        yield b'{"job_type": "burn", "params": {'
        for i in range(3000):
            yield f'"p{i}": {i}, '.encode()
        yield b'"x": 1}, "est_duration_s": 2, "max_delay_h": 1}'

    r = srv.client.post("/jobs", content=chunks(), headers=alice | {"Content-Type": "application/json"})
    assert r.status_code == 413


def test_queue_cap(tmp_path):
    s = Server(tmp_path, max_queued_per_user=2)
    with s.client:
        alice = s.user("alice")
        s.submit(alice)
        s.submit(alice)
        assert s.client.post("/jobs", json=BURN, headers=alice).status_code == 429


def test_missing_choose_slot_gives_a_clear_501(tmp_path):
    def not_written(request, forecast_mean, params):
        raise NotImplementedError("Phase 3: write choose_slot yourself, see the docstring")

    s = Server(tmp_path, choose=not_written)
    with s.client:
        alice = s.user("alice")
        r = s.client.post("/jobs", json=BURN, headers=alice)
        assert r.status_code == 501 and "write choose_slot yourself" in r.json()["detail"]


def test_submit_returns_the_plan(srv):
    plan = srv.submit(srv.user("alice"))
    assert plan["deferred"] is True and plan["reason"] == "deferred"  # fake_choose: +1 h
    assert plan["planned_start"].startswith("2026-11-02T07:00")
    assert plan["latest_start"].startswith("2026-11-02T10:00")
    assert plan["est_duration_sim_s"] == 2 * SCALE


# ---------------------------------------------------------------- claim, leases, cancel
def test_claim_waits_for_planned_start(srv):
    alice = srv.user("alice")
    job = srv.submit(alice)["job_id"]
    assert srv.claim().status_code == 204
    srv.advance_sim(timedelta(hours=1))
    r = srv.claim()
    assert r.status_code == 200 and r.json()["job_id"] == job
    assert r.json()["params"] == BURN["params"] and r.json()["job_type"] == "burn"
    assert srv.claim().status_code == 204
    assert srv.client.delete(f"/jobs/{job}", headers=alice).status_code == 409  # running: too late


def test_lease_expiry_requeues_then_fails(srv):
    alice = srv.user("alice")
    job = srv.submit(alice)["job_id"]
    srv.advance_sim(timedelta(hours=1))
    srv.claim("w1")
    srv.rt.t += 3 * 2 + 60 + 1  # past the lease (3 x est + 60 real s)
    assert srv.state.tick().lease_changes == [(job, "queued")]
    assert srv.claim("w2").status_code == 200
    assert srv.result(job, agent_id="w1").status_code == 409  # w1 lost the lease
    srv.rt.t += 3 * 2 + 60 + 1
    assert srv.state.tick().lease_changes == [(job, "failed")]
    assert srv.client.get(f"/jobs/{job}", headers=alice).json()["status"] == "failed"


# ---------------------------------------------------------------- results
def claimed_job(srv, key):
    job = srv.submit(key)["job_id"]
    srv.advance_sim(timedelta(hours=1))
    assert srv.claim().status_code == 200
    return job


@pytest.mark.parametrize("change", [
    {"cpu_share": 1.5}, {"cpu_share": -0.1}, {"energy_kwh_job": -1.0}, {"energy_unc_kwh": -1e-9},
    {"samples": -1}, {"exit_code": "0"}, {"unexpected": True}, {"duration_s": -2},
])
def test_result_validation(srv, change):
    job = claimed_job(srv, srv.user("alice"))
    assert srv.result(job, **change).status_code == 422


def test_result_rejects_nan_and_time_travel(srv):
    job = claimed_job(srv, srv.user("alice"))
    body = ('{"job_id": "%s", "agent_id": "laptop", "exit_code": 0, "started_real": 1, "ended_real": 2, '
            '"duration_s": 1, "energy_kwh_job": NaN, "energy_unc_kwh": 0, "cpu_share": 0.5, "job_cpu_s": 1, '
            '"system_busy_cpu_s": 1, "samples": 1, "scope": "cpu_package"}' % job)
    r = srv.client.post("/agent/result", content=body, headers=A_HDR | {"Content-Type": "application/json"})
    assert r.status_code == 422
    assert srv.result(job, started_real=srv.rt.t, ended_real=srv.rt.t - 1).status_code == 422


def test_result_for_unknown_job_or_other_agent(srv):
    job = claimed_job(srv, srv.user("alice"))
    assert srv.result("job-nope").status_code == 404
    assert srv.result(job, agent_id="impostor").status_code == 409


def test_failed_job_is_not_settled(srv):
    alice = srv.user("alice")
    job = claimed_job(srv, alice)
    r = srv.result(job, exit_code=1)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    detail = srv.client.get(f"/jobs/{job}", headers=alice).json()
    assert detail["status"] == "failed" and detail["settlement"] is None
    me = srv.client.get("/me", headers=alice).json()
    assert me["balance"] == 100.0 and [e["kind"] for e in me["last_entries"]] == ["allowance"]


def test_result_is_idempotent(srv):
    alice = srv.user("alice")
    job = claimed_job(srv, alice)
    first = srv.result(job)
    assert first.status_code == 200 and first.json()["status"] == "done"
    balance = srv.client.get("/me", headers=alice).json()["balance"]
    again = srv.result(job, energy_kwh_job=0.9)  # different numbers: ignored
    assert again.status_code == 200 and again.json()["already_settled"] is True
    assert again.json()["settlement"]["net"] == first.json()["settlement"]["net"]
    assert srv.client.get("/me", headers=alice).json()["balance"] == balance


# ---------------------------------------------------------------- telemetry and admin state
def test_telemetry_batches_keep_last_n_and_follow_overrides(tmp_path):
    s = Server(tmp_path, telemetry_keep=5)
    with s.client:
        alice = s.user("alice")
        samples = [{"ts_real": s.rt.t + i, "watts": 10.0 + i} for i in range(8)]
        r = s.client.post("/agent/telemetry", json={"agent_id": "laptop", "samples": samples}, headers=A_HDR)
        assert r.status_code == 200 and r.json() == {"stored": 8}
        latest = s.client.get("/telemetry/latest?n=60", headers=alice).json()
        assert [row["watts"] for row in latest] == [17.0, 16.0, 15.0, 14.0, 13.0]  # newest first, 5 kept
        assert s.client.get("/telemetry/latest", headers={}).status_code == 401
        assert s.client.get("/telemetry/latest", headers=ADM_HDR).status_code == 200

        s.client.post("/admin/intensity", json={"state": "dirty"}, headers=ADM_HDR)
        s.rt.t += 30  # samples now lie after the override's start in sim time
        s.client.post("/agent/telemetry", json={"agent_id": "laptop", "samples": [{"ts_real": s.rt.t, "watts": 36.0}]},
                      headers=A_HDR)
        newest = s.client.get("/telemetry/latest?n=1", headers=alice).json()[0]
        assert newest["g_per_kwh"] == 700.0
        assert newest["gco2e_per_s"] == pytest.approx(36.0 / 3.6e6 * 700.0)


def test_admin_state(srv):
    alice = srv.user("alice")
    srv.user("bob", allowance=10)
    srv.submit(alice)
    st = srv.client.get("/admin/state", headers=ADM_HDR).json()
    assert st["time_scale"] == SCALE and st["sim_now"].startswith("2026-11-02T06:00")
    assert len(st["queue"]) == 1 and st["queue"][0]["ready"] is False and st["running"] == []
    assert {u["user_id"]: u["tier"] for u in st["users"]} == {"alice": "green", "bob": "red"}


# ---------------------------------------------------------------- end to end (in-process)
def test_end_to_end_rebate_then_backfire_and_tier_change(srv):
    """Submit while dirty -> flip to green -> run -> rebate; then flip to dirty
    before a second job's slot -> backfire. Everything but choose_slot is real."""
    alice = srv.user("alice")

    # 1. dirty now, submit; fake_choose plans for +1 h
    srv.client.post("/admin/intensity", json={"state": "dirty"}, headers=ADM_HDR)
    job1 = srv.submit(alice)["job_id"]
    # 2. 40 sim minutes later the grid turns green; the counterfactual [t0, t0+20 min] stays dirty
    srv.advance_sim(timedelta(minutes=40))
    srv.client.post("/admin/intensity", json={"state": "green"}, headers=ADM_HDR)
    srv.advance_sim(timedelta(minutes=20))
    srv.state.tick()
    assert srv.claim().json()["job_id"] == job1
    s1 = srv.result(job1).json()["settlement"]  # 2 real s = 20 sim min of run, at 80 g/kWh
    assert s1["zone"] == "saving" and s1["rebate"] > 0
    assert s1["fee"] == pytest.approx(1000 * 0.0005 * 80)
    assert s1["rebate"] == pytest.approx(0.6 * s1["fee"])  # the cap binds (700 -> 80 is a big saving)
    me = srv.client.get("/me", headers=alice).json()
    kinds = [e["kind"] for e in me["last_entries"]]
    assert kinds == ["rebate", "fee", "decay", "allowance"]  # newest first; decay applied before booking
    decay = -me["last_entries"][2]["amount"]
    assert 0 < decay < 0.1  # one sim hour of a 30-day half-life on 100 credits
    assert me["balance"] == pytest.approx(100 - decay - s1["net"], abs=1e-6)
    assert me["tier"] == "standard"  # about 84 credits

    # 3. second job submitted while green; flip to dirty before its slot -> backfire
    job2 = srv.submit(alice)["job_id"]
    srv.advance_sim(timedelta(minutes=40))
    srv.client.post("/admin/intensity", json={"state": "dirty"}, headers=ADM_HDR)
    srv.advance_sim(timedelta(minutes=20))
    srv.state.tick()
    assert srv.claim().json()["job_id"] == job2
    s2 = srv.result(job2).json()["settlement"]
    assert s2["zone"] == "backfire" and s2["rebate"] == 0
    assert s2["fee"] == pytest.approx(1000 * 0.0005 * 700)

    st = srv.client.get("/admin/state", headers=ADM_HDR).json()
    assert [x["zone"] for x in st["settlements"]] == ["backfire", "saving"]
    assert srv.client.get("/me", headers=alice).json()["tier"] == "red"  # 84 - 350 < 20: debt
