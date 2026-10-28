"""M3: migration, clock/override persistence, dispatch order, planner, queue, claim, leases."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from greensettle.intensity.provider import SimulatedCurve
from greensettle.intensity.timeline import ActualProvider, ForecastProvider, mean_intensity
from greensettle.ledger import db as ledger_db
from greensettle.ledger import migrations
from greensettle.scheduler import queue
from greensettle.ledger.config import TierInfo
from greensettle.scheduler.dispatch import (
    QueuedJob,
    can_start,
    dispatch_order,
    pick_next,
)
from greensettle.scheduler.planner import plan_job
from greensettle.scheduler.slots import SchedulerParams
from greensettle.server import store
from tests.fakes import FakeRealTime, fake_choose, start_now_choose

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
H = timedelta(hours=1)
BURN = {"seconds": 2, "workers": 1}


def providers() -> tuple[ForecastProvider, ActualProvider]:
    curve = SimulatedCurve(tz=UTC)
    return ForecastProvider(curve), ActualProvider(curve)


@pytest.fixture
def conn():
    c = store.open_db(":memory:")
    for user in ("alice", "bob"):
        ledger_db.create_user(c, user, T0)
    yield c
    c.close()


def submit(conn, user="alice", sim_now=T0, max_delay_h=4.0, choose=fake_choose, est=10.0, **kw):
    forecast, actual = providers()
    return queue.submit_job(
        conn, user_id=user, job_type="burn", params=BURN, est_duration_s=est, max_delay_h=max_delay_h,
        sim_now=sim_now, time_scale=600, forecast=forecast, actual=actual, choose=choose, **kw,
    )


# ---------------------------------------------------------------- migration
def test_migrates_an_existing_phase2_database(tmp_path):
    path = tmp_path / "g.db"
    # A database exactly as the first Phase 2 code left it: schema v1, user_version 0.
    old = sqlite3.connect(path)
    old.executescript(migrations.SCHEMA_V1)
    old.execute("INSERT INTO users VALUES ('alice', '2026-11-02T06:00:00+00:00', '2026-11-02T06:00:00+00:00')")
    old.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro, period_key) "
                "VALUES ('alice', '2026-11-02T06:00:00+00:00', 'allowance', 100000000, '2026-11')")
    old.commit()
    old.close()

    conn = store.open_db(path)
    assert store.schema_version(conn) == 4
    assert ledger_db.balance_micro(conn, "alice") == 100_000_000  # data kept
    assert ledger_db.last_decay_at(conn, "alice") == T0  # decayed_through renamed, value kept
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"job_type", "status", "lease_expires_real", "plan_json", "declared_window_h"} <= job_cols
    assert "api_key_hash" in {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"telemetry", "intensity_overrides", "meta"} <= tables
    conn.close()
    assert store.schema_version(store.open_db(path)) == 4  # reopening is a no-op


def test_v2_to_v3_keeps_settlements_and_drops_low_high(tmp_path):
    path = tmp_path / "g.db"
    old = sqlite3.connect(path)
    old.execute("PRAGMA foreign_keys = ON")
    old.executescript(migrations.SCHEMA_V1)
    old.execute("PRAGMA user_version = 1")
    old.executescript(migrations.MIGRATION_V2)
    old.execute("INSERT INTO users (user_id, created_at, decayed_through) VALUES ('alice', 'x', 'x')")
    old.execute("INSERT INTO jobs (job_id, user_id, submitted_at, i_submit, i_submit_unc) VALUES ('j1','alice','x',1,1)")
    old.execute("INSERT INTO settlements VALUES ('j1','alice','x',1,1,1,1,1,1, 0.1,0.01,0.09,0.11,"
                "'saving', 10,5,0,5,'{}')")
    old.commit()
    old.close()
    conn = store.open_db(path)
    row = conn.execute("SELECT * FROM settlements WHERE job_id='j1'").fetchone()
    assert (row["zone"], row["saved_g"], row["saved_unc_g"], row["net_micro"]) == ("saving", 0.1, 0.01, 5)
    assert "saved_low_g" not in row.keys()


def test_v4_refuses_old_adjustment_rows_and_leaves_v3_intact(tmp_path):
    path = tmp_path / "g.db"
    old = sqlite3.connect(path)
    old.executescript(migrations.SCHEMA_V1)
    old.execute("PRAGMA user_version = 1")
    for step in (migrations.MIGRATION_V2, migrations.MIGRATION_V3):
        old.executescript(step)
    old.execute("INSERT INTO users (user_id, created_at, last_decay_at) VALUES ('alice', 'x', 'x')")
    old.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('alice', 'x', 'adjustment', 5)")
    old.commit()
    old.close()
    with pytest.raises(sqlite3.IntegrityError):
        store.open_db(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 3  # nothing half-done
    assert check.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_newer_database_is_refused():
    conn = store.open_db(":memory:")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        store.migrate(conn)


def test_job_status_check_constraint(conn):
    submit(conn, job_id="j1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET status = 'exploded' WHERE job_id = 'j1'")


# ---------------------------------------------------------------- clock and overrides persistence
def test_clock_is_saved_and_resumed_across_restarts(tmp_path):
    path = tmp_path / "g.db"
    rt = FakeRealTime()
    c1 = store.open_db(path)
    clock = store.load_clock(c1, scale=600, real_time=rt, sim_start=T0)
    rt.t += 6
    assert clock.now() == T0 + H
    c1.close()

    rt.t += 6  # server down; sim time keeps going at the old scale
    c2 = store.open_db(path)
    resumed = store.load_clock(c2, scale=60, real_time=rt, sim_start=datetime(2030, 1, 1, tzinfo=UTC))
    assert resumed.now() == T0 + 2 * H  # sim_start is ignored on restart
    rt.t += 60
    assert resumed.now() == T0 + 3 * H  # new scale x60
    store.set_clock_scale(c2, resumed, 600)
    assert store.meta_get(c2, "clock_scale") == "600"


def test_override_timeline_persists_and_cannot_rewrite_history(conn):
    store.append_override(conn, T0 + H, "dirty")
    store.append_override(conn, T0 + 2 * H, "off")
    with pytest.raises(ValueError):
        store.append_override(conn, T0, "green")  # before the latest override
    with pytest.raises(ValueError):
        store.append_override(conn, T0 + 3 * H, "purple")
    timeline = store.load_timeline(conn)
    assert timeline.state_at(T0) == "off"
    assert timeline.state_at(T0 + 90 * timedelta(minutes=1)) == "dirty"
    assert timeline.state_at(T0 + 3 * H) == "off"


# ---------------------------------------------------------------- dispatch_order / can_start
GOLD = TierInfo("gold", 100, priority=2, max_concurrent=2)
BRONZE = TierInfo("bronze", 0, priority=0, max_concurrent=1)
TIERS = {"alice": BRONZE, "bob": GOLD, "carol": BRONZE}


def tier_of(user: str) -> TierInfo:
    return TIERS[user]


def qjob(job_id, user, planned_h, latest_h, submitted_h=0.0, seq=0) -> QueuedJob:
    return QueuedJob(job_id, user, T0 + planned_h * H, T0 + latest_h * H, T0 + submitted_h * H, seq)


def ids(jobs) -> list[str]:
    return [j.job_id for j in jobs]


def test_only_ready_jobs_are_dispatched():
    jobs = [qjob("later", "alice", 3, 6), qjob("ready", "alice", 1, 6)]
    assert ids(dispatch_order(jobs, T0 + 2 * H, tier_of)) == ["ready"]
    assert ids(dispatch_order(jobs, T0 + 3 * H, tier_of)) == ["ready", "later"]  # planned_start <= now


def test_higher_tier_first_then_planned_start_then_submission():
    jobs = [
        qjob("bronze-early", "alice", 0, 10, seq=1),
        qjob("gold-late", "bob", 1, 10, seq=2),
        qjob("gold-early", "bob", 0.5, 10, seq=3),
        qjob("bronze-early-2", "carol", 0, 10, submitted_h=-1, seq=4),  # submitted earlier
        qjob("bronze-early-3", "carol", 0, 10, submitted_h=-1, seq=5),  # same time: seq breaks the tie
    ]
    assert ids(dispatch_order(jobs, T0 + 2 * H, tier_of)) == [
        "gold-early", "gold-late", "bronze-early-2", "bronze-early-3", "bronze-early",
    ]


def test_starvation_guard_overdue_jobs_go_first_ignoring_tier():
    jobs = [
        qjob("gold", "bob", 1, 10, seq=1),
        qjob("bronze-overdue", "alice", 0, 2, seq=2),  # window ended at T0 + 2 h
    ]
    assert ids(dispatch_order(jobs, T0 + 2 * H, tier_of)) == ["gold", "bronze-overdue"]  # not past yet
    assert ids(dispatch_order(jobs, T0 + 3 * H, tier_of)) == ["bronze-overdue", "gold"]


def test_overdue_jobs_among_themselves_ignore_tier():
    jobs = [qjob("gold-overdue", "bob", 1, 1.5, seq=1), qjob("bronze-overdue", "alice", 0, 1, seq=2)]
    assert ids(dispatch_order(jobs, T0 + 5 * H, tier_of)) == ["bronze-overdue", "gold-overdue"]


def test_can_start_respects_global_and_tier_caps():
    j_alice, j_bob = qjob("a", "alice", 0, 1), qjob("b", "bob", 0, 1)
    assert can_start(j_alice, 0, {}, tier_of, 2)
    assert not can_start(j_alice, 2, {}, tier_of, 2)  # global cap
    assert not can_start(j_alice, 1, {"alice": 1}, tier_of, 2)  # bronze allows 1
    assert can_start(j_bob, 1, {"bob": 1}, tier_of, 3)  # gold allows 2
    assert not can_start(j_bob, 2, {"bob": 2}, tier_of, 3)


def test_pick_next_skips_a_user_at_their_limit():
    jobs = [qjob("alice-1", "alice", 0, 10, seq=1), qjob("carol-1", "carol", 0, 10, seq=2)]
    assert pick_next(jobs, T0, 1, {"alice": 1}, tier_of, 2).job_id == "carol-1"
    assert pick_next(jobs, T0, 2, {"alice": 1, "x": 1}, tier_of, 2) is None


# ---------------------------------------------------------------- planner and submit
def test_plan_job_converts_real_duration_to_sim_and_binds_the_forecast():
    forecast, _ = providers()
    seen = {}

    def spy(request, forecast_mean, params):
        seen["request"], seen["fm"] = request, forecast_mean
        return start_now_choose(request, forecast_mean, params)

    plan = plan_job(T0, 6.0, 25.0, 600, forecast, SchedulerParams(), choose=spy)
    assert seen["request"].est_duration_s == 25 * 600
    assert seen["request"].submit_time == T0 and seen["request"].max_delay_h == 6.0
    assert seen["fm"](T0 + H, T0 + 2 * H) == forecast.mean(T0 + H, T0 + 2 * H, issued_at=T0)
    assert plan.latest_start == T0 + 6 * H and plan.est_duration_sim_s == 15_000


def test_submit_stores_plan_and_provisional_counterfactual(conn):
    res = submit(conn, job_id="j1")
    job = queue.get_job(conn, "j1", "alice")
    assert job["status"] == "queued" and job["job_type"] == "burn" and job["params"] == BURN
    assert store.parse_ts(job["planned_start"]) == T0 + H  # fake_choose defers by 1 h
    assert store.parse_ts(job["latest_start"]) == T0 + 4 * H
    assert job["plan"]["reason"] == "deferred" and len(job["plan"]["candidates"]) == 2
    assert job["max_delay_h"] == 4.0 and job["est_duration_s"] == 10.0
    _, actual = providers()
    assert job["i_submit"] == pytest.approx(mean_intensity(actual, T0, T0 + timedelta(seconds=6000))[0])
    assert "provisional" in job["i_submit_source"]
    assert res.plan.choice.start == T0 + H


def test_submit_rejections(conn):
    forecast, actual = providers()
    base = dict(user_id="alice", job_type="burn", params=BURN, est_duration_s=10, max_delay_h=1,
                sim_now=T0, time_scale=600, forecast=forecast, actual=actual, choose=fake_choose)
    for change in ({"job_type": "rm"}, {"params": {"seconds": 99, "workers": 1}}, {"est_duration_s": 121},
                   {"max_delay_h": -1}, {"max_delay_h": 49}, {"max_delay_h": float("inf")},
                   {"user_id": "mallory"}):
        with pytest.raises(queue.SubmitError):
            queue.submit_job(conn, **{**base, **change})
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_failed_planning_stores_nothing(conn):
    def broken(request, forecast_mean, params):
        raise NotImplementedError("Phase 3: write choose_slot yourself, see the docstring")

    with pytest.raises(NotImplementedError):
        submit(conn, choose=broken)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_queued_cap_per_user(conn):
    for i in range(3):
        submit(conn, max_queued=3, job_id=f"a{i}")
    with pytest.raises(queue.QueueFullError):
        submit(conn, max_queued=3)
    submit(conn, user="bob", max_queued=3)  # other users unaffected
    queue.cancel_job(conn, "a0", "alice")
    submit(conn, max_queued=3)  # a slot freed up


def test_get_and_cancel_are_owner_only(conn):
    submit(conn, job_id="j1")
    with pytest.raises(queue.JobNotFound):
        queue.get_job(conn, "j1", "bob")
    with pytest.raises(queue.JobNotFound):
        queue.cancel_job(conn, "j1", "bob")
    with pytest.raises(queue.JobNotFound):
        queue.get_job(conn, "nope")
    queue.cancel_job(conn, "j1", "alice")
    assert queue.get_job(conn, "j1")["status"] == "cancelled"
    with pytest.raises(ValueError, match="only queued"):
        queue.cancel_job(conn, "j1", "alice")


# ---------------------------------------------------------------- claim and leases
def test_claim_waits_for_the_planned_start(conn):
    submit(conn, job_id="j1")  # planned for T0 + 1 h
    assert queue.claim_job(conn, "laptop", T0 + 30 * timedelta(minutes=1), real_now=1000.0) is None
    claim = queue.claim_job(conn, "laptop", T0 + H, real_now=1000.0)
    assert claim.job_id == "j1" and claim.job_type == "burn" and claim.params == BURN
    assert claim.lease_expires_real == 1000.0 + 3 * 10 + 60
    job = queue.get_job(conn, "j1")
    assert job["status"] == "running" and job["claimed_by"] == "laptop" and job["attempts"] == 1
    assert queue.claim_job(conn, "laptop", T0 + H, real_now=1001.0) is None  # nothing left


def test_claim_respects_global_concurrency(conn):
    for i in range(3):
        submit(conn, user=("alice", "bob", "bob")[i], choose=start_now_choose, job_id=f"j{i}")
    gold_for_all = lambda u: TierInfo("x", 0, priority=0, max_concurrent=5)  # noqa: E731
    got = [queue.claim_job(conn, "w", T0, 0.0, gold_for_all, max_concurrent_jobs=2) for _ in range(3)]
    assert [c.job_id for c in got if c] == ["j0", "j1"] and got[2] is None


def test_atomic_claim_two_threads_one_job(tmp_path):
    path = tmp_path / "g.db"
    setup = store.open_db(path)
    ledger_db.create_user(setup, "alice", T0)
    for round_ in range(15):
        submit(setup, choose=start_now_choose, job_id=f"r{round_}")
        barrier = threading.Barrier(2)
        winners: list[str] = []
        errors: list[BaseException] = []

        def worker(agent: str) -> None:
            c = store.open_db(path)  # one connection per thread
            try:
                barrier.wait()
                if queue.claim_job(c, agent, T0, 0.0, lambda u: TierInfo("x", 0, priority=0, max_concurrent=99), 99):
                    winners.append(agent)
            except BaseException as exc:  # surfaced below
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(winners) == 1, f"round {round_}: {winners}"
    assert setup.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0] == 15


def test_lease_expiry_requeues_once_then_fails(conn):
    submit(conn, choose=start_now_choose, job_id="j1", est=10.0)
    lease = 3 * 10 + 60
    queue.claim_job(conn, "w1", T0, real_now=0.0)
    assert queue.tick(conn, T0, real_now=lease - 1).lease_changes == []  # still valid
    assert queue.tick(conn, T0, real_now=lease + 1).lease_changes == [("j1", "queued")]
    job = queue.get_job(conn, "j1")
    assert job["status"] == "queued" and job["claimed_by"] is None and job["attempts"] == 1

    queue.claim_job(conn, "w2", T0, real_now=200.0)
    assert queue.get_job(conn, "j1")["attempts"] == 2
    assert queue.tick(conn, T0, real_now=200.0 + lease + 1).lease_changes == [("j1", "failed")]
    job = queue.get_job(conn, "j1")
    assert job["status"] == "failed" and job["failure_reason"] == "lease expired twice"
    assert queue.claim_job(conn, "w3", T0, real_now=1000.0) is None


def test_plan_json_is_valid_json(conn):
    submit(conn, job_id="j1")
    raw = conn.execute("SELECT plan_json FROM jobs WHERE job_id='j1'").fetchone()[0]
    assert json.loads(raw)["deferred"] is True


# ---------------------------------------------------------------- real tiers and decay at tick time
M = 1_000_000


def test_claim_uses_ledger_tiers_priority_and_limits(conn):
    # alice: green (100 credits, priority 2, 2 at once); bob: red (0 credits, priority 0, 1 at once)
    ledger_db.grant_allowance(conn, "alice", T0, period="2026-11")
    submit(conn, user="bob", choose=start_now_choose, job_id="b1")
    submit(conn, user="bob", choose=start_now_choose, job_id="b2")
    submit(conn, user="alice", choose=start_now_choose, job_id="a1")
    submit(conn, user="alice", choose=start_now_choose, job_id="a2")
    got = [queue.claim_job(conn, "w", T0, 0.0, max_concurrent_jobs=4) for _ in range(4)]
    # green alice first (both jobs), then red bob limited to one running job
    assert [c.job_id if c else None for c in got] == ["a1", "a2", "b1", None]


def test_tick_applies_decay_for_users_with_ready_jobs_and_changes_the_tier(conn):
    ledger_db.grant_allowance(conn, "alice", T0, period="2026-11")
    ledger_db.grant_allowance(conn, "bob", T0, period="2026-11")
    submit(conn, user="alice", job_id="a1")  # fake_choose: planned for T0 + 1 h
    later = T0 + timedelta(days=30)
    report = queue.tick(conn, later, real_now=0.0)
    assert report.decayed_micro == {"alice": 50 * M}  # bob has no ready job: not decayed by the tick
    assert ledger_db.tier_of_user(conn, "alice").name == "standard"
    assert ledger_db.tier_of_user(conn, "bob").name == "green"
    assert queue.tick(conn, T0 + H, real_now=0.0).decayed_micro == {"alice": 0}  # time went backwards: no-op


def test_tick_ignores_jobs_that_are_not_ready_yet(conn):
    ledger_db.grant_allowance(conn, "alice", T0, period="2026-11")
    submit(conn, user="alice", job_id="a1")  # planned for T0 + 1 h
    assert queue.tick(conn, T0 + timedelta(minutes=30), real_now=0.0).decayed_micro == {}
