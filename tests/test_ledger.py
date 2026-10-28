"""Ledger: schema rules, decay, tiers, allowance, rounding and settle_job."""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from greensettle.ledger import credits, db
from greensettle.ledger.config import DEFAULT_CONFIG, DEFAULT_TIERS, LedgerConfig, tier_for
from greensettle.ledger.settle import record_offline_job, settle_job
from greensettle.settlement.rules import Settlement, SettlementParams, compute_settlement

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)
M = 1_000_000  # micro-credits per credit


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.create_user(c, "alice", T0)
    yield c
    c.close()


def rows(conn, user="alice"):
    return [(r["kind"], r["amount_micro"]) for r in
            conn.execute("SELECT kind, amount_micro FROM ledger WHERE user_id=? ORDER BY entry_id", (user,))]


# ---------------------------------------------------------------- schema rules
def test_schema_is_created_and_reopening_is_safe(tmp_path):
    path = tmp_path / "sub" / "g.db"
    c = db.connect(path)
    db.create_user(c, "bob", T0)
    c.close()
    c = db.connect(path)
    assert db.user_exists(c, "bob")
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"users", "ledger", "jobs", "settlements", "ledger_with_balance", "ledger_no_update",
            "ledger_no_delete", "ledger_one_per_period", "ledger_one_per_job"} <= names
    settlement_cols = {r[1] for r in c.execute("PRAGMA table_info(settlements)")}
    assert {"zone", "saved_g", "saved_unc_g", "params_json"} <= settlement_cols
    assert not {"saved_low_g", "saved_high_g"} & settlement_cols
    assert "last_decay_at" in {r[1] for r in c.execute("PRAGMA table_info(users)")}


def test_ledger_rejects_update_and_delete(conn):
    db.grant_allowance(conn, "alice", T0, 100, "2026-11")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger SET amount_micro = 999")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger")
    assert db.balance_micro(conn, "alice") == 100 * M


def test_kind_check_and_integer_amounts_enforced_by_sqlite(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('alice', 'x', 'gift', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('alice', 'x', 'fee', 1.5)")
    with pytest.raises(sqlite3.IntegrityError):  # foreign key: unknown user
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('mallory', 'x', 'fee', -1)")


def test_unique_job_kind_enforced_by_sqlite(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro, job_id) VALUES "
                     "('alice','x','fee',-1,'j1'), ('alice','x','fee',-1,'j1')")


def test_append_entry_validation(conn):
    with pytest.raises(ValueError):
        db.append_entry(conn, "alice", T0, "gift", 1)
    with pytest.raises(TypeError):
        db.append_entry(conn, "alice", T0, "fee", -1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown user"):
        db.append_entry(conn, "nobody", T0, "fee", -1)


def test_immediate_rolls_back_everything_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.immediate(conn):
            db.append_entry(conn, "alice", T0, "adjustment", 5 * M)
            raise RuntimeError("boom")
    assert db.balance_micro(conn, "alice") == 0


# ---------------------------------------------------------------- users and time
def test_create_user(conn):
    with pytest.raises(ValueError, match="already exists"):
        db.create_user(conn, "alice", T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        db.create_user(conn, "carol", datetime(2026, 1, 1))
    ist = timezone(timedelta(hours=5, minutes=30))
    db.create_user(conn, "dev", datetime(2026, 1, 1, 10, 0, tzinfo=ist))
    row = conn.execute("SELECT created_at, last_decay_at FROM users WHERE user_id='dev'").fetchone()
    assert row["created_at"] == "2026-01-01T04:30:00+00:00"
    assert row["last_decay_at"] == row["created_at"]  # new users: last decay = creation


def test_balance_is_sum_and_view_gives_running_balance(conn):
    db.grant_allowance(conn, "alice", T0, 100, "2026-11")
    with db.immediate(conn):
        db.append_entry(conn, "alice", T0, "fee", -14 * M, job_id="j1")
        db.append_entry(conn, "alice", T0, "rebate", 8_400_000, job_id="j1")
    assert db.balance_micro(conn, "alice") == 94_400_000
    assert db.balance_micro(conn, "nobody") == 0
    running = [r["balance_micro"] for r in conn.execute(
        "SELECT balance_micro FROM ledger_with_balance WHERE user_id='alice' ORDER BY entry_id")]
    assert running == [100 * M, 86 * M, 94_400_000]


def test_no_float_drift_over_many_entries(conn):
    with db.immediate(conn):
        for _ in range(10_000):
            db.append_entry(conn, "alice", T0, "adjustment", credits.to_micro(0.1))
    assert db.balance_micro(conn, "alice") == 1000 * M
    total = 0.0
    for _ in range(10_000):
        total += 0.1
    assert total != 1000.0  # the problem integers avoid


# ---------------------------------------------------------------- decay
def test_decay_amount_formula():
    assert db.decay_amount_micro(100 * M, 30, 30) == 50 * M  # one half-life
    assert db.decay_amount_micro(100 * M, 60, 30) == 75 * M  # two half-lives
    assert db.decay_amount_micro(100 * M, 1, 30) == math.floor(100 * M * (1 - 0.5 ** (1 / 30)))
    assert db.decay_amount_micro(100 * M, 0, 30) == 0
    assert db.decay_amount_micro(100 * M, -5, 30) == 0
    assert db.decay_amount_micro(0, 30, 30) == 0
    assert db.decay_amount_micro(-50 * M, 30, 30) == 0  # debt does not decay
    assert db.decay_amount_micro(1, 0.001, 30) == 0  # less than one micro-credit: floored to 0


def test_apply_decay_writes_a_row_and_moves_last_decay_at(conn):
    db.grant_allowance(conn, "alice", T0, 100, "2026-11")
    assert db.apply_decay(conn, "alice", T0 + 30 * DAY) == 50 * M
    assert rows(conn)[-1] == ("decay", -50 * M)
    assert db.balance_micro(conn, "alice") == 50 * M
    assert db.last_decay_at(conn, "alice") == T0 + 30 * DAY
    # Another 30 days: 50 -> 25. Decay is measured from the LAST decay, not creation.
    assert db.apply_decay(conn, "alice", T0 + 60 * DAY) == 25 * M


def test_decay_does_nothing_when_time_has_not_moved_forward(conn):
    db.grant_allowance(conn, "alice", T0, 100, "2026-11")
    before = rows(conn)
    assert db.apply_decay(conn, "alice", T0) == 0
    assert db.apply_decay(conn, "alice", T0 - DAY) == 0  # clock never goes backwards
    assert rows(conn) == before
    assert db.last_decay_at(conn, "alice") == T0  # untouched


def test_decay_updates_last_decay_at_even_without_a_row(conn):
    # Zero balance: nothing to decay, but the clock still moves on.
    assert db.apply_decay(conn, "alice", T0 + 10 * DAY) == 0
    assert rows(conn) == []
    assert db.last_decay_at(conn, "alice") == T0 + 10 * DAY


def test_debt_does_not_decay(conn):
    with db.immediate(conn):
        db.append_entry(conn, "alice", T0, "adjustment", -30 * M)
    assert db.apply_decay(conn, "alice", T0 + 90 * DAY) == 0
    assert db.balance_micro(conn, "alice") == -30 * M
    assert db.last_decay_at(conn, "alice") == T0 + 90 * DAY


def test_lazy_decay_equals_one_big_decay():
    # Decaying daily for 30 days ends (within rounding) where one 30-day decay ends.
    daily, once = db.connect(":memory:"), db.connect(":memory:")
    for c in (daily, once):
        db.create_user(c, "u", T0)
        db.grant_allowance(c, "u", T0, 100, "2026-11")
    for d in range(1, 31):
        db.apply_decay(daily, "u", T0 + d * DAY)
    db.apply_decay(once, "u", T0 + 30 * DAY)
    assert abs(db.balance_micro(daily, "u") - db.balance_micro(once, "u")) <= 30  # <= 1 micro lost per step


# ---------------------------------------------------------------- tiers
@pytest.mark.parametrize("balance, tier", [
    (-1e6, "red"), (-0.000001, "red"), (0, "red"), (19.999999, "red"),
    (20, "standard"), (20.000001, "standard"), (99.999999, "standard"),
    (100, "green"), (1e9, "green"),
])
def test_tier_for_boundaries(balance, tier):
    assert tier_for(balance).name == tier


def test_tier_table_values():
    assert [(t.name, t.priority, t.max_concurrent) for t in DEFAULT_TIERS] == [
        ("red", 0, 1), ("standard", 1, 1), ("green", 2, 2)]


def test_tier_of_user_follows_the_ledger(conn):
    assert db.tier_of_user(conn, "alice").name == "red"
    db.grant_allowance(conn, "alice", T0, 100, "2026-11")
    assert db.tier_of_user(conn, "alice").name == "green"
    db.apply_decay(conn, "alice", T0 + 30 * DAY)  # 100 -> 50
    assert db.tier_of_user(conn, "alice").name == "standard"


# ---------------------------------------------------------------- allowance
def test_allowance_defaults_month_and_is_idempotent(conn):
    first = db.grant_allowance(conn, "alice", T0)
    assert first.granted and first.period == "2026-11" and first.amount_micro == 100 * M
    again = db.grant_allowance(conn, "alice", T0 + DAY)
    assert not again.granted and again.message == "already granted for 2026-11" and again.entry_id is None
    assert db.balance_micro(conn, "alice") == 100 * M
    assert db.last_decay_at(conn, "alice") == T0  # the no-op wrote nothing, not even decay
    dec = db.grant_allowance(conn, "alice", T0 + 30 * DAY)  # December
    assert dec.granted and dec.period == "2026-12"
    assert dec.decay_micro == 50 * M  # decay applied BEFORE the grant
    assert db.balance_micro(conn, "alice") == 150 * M


def test_allowance_period_uses_utc_month():
    late_ist = datetime(2026, 12, 1, 2, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert db.month_period(late_ist) == "2026-11"  # still 30 Nov in UTC


def test_allowance_validation(conn):
    for period in ("2026-W45", "2026-13", "26-01", "2026-1", ""):
        if period == "":
            continue  # empty means "use the default month"
        with pytest.raises(ValueError):
            db.grant_allowance(conn, "alice", T0, 10, period)
    for amount in (0, -5, math.nan, math.inf):
        with pytest.raises(ValueError):
            db.grant_allowance(conn, "alice", T0, amount, "2026-11")
    with pytest.raises(ValueError, match="unknown user"):
        db.grant_allowance(conn, "nobody", T0, 10, "2026-11")


def test_allowance_periods_are_per_user(conn):
    db.create_user(conn, "bob", T0)
    assert db.grant_allowance(conn, "alice", T0, 50, "2026-11").granted
    assert db.grant_allowance(conn, "bob", T0, 50, "2026-11").granted


def test_allowance_helps_a_user_recover_from_debt(conn):
    with db.immediate(conn):
        db.append_entry(conn, "alice", T0, "adjustment", -30 * M)
    assert db.tier_of_user(conn, "alice").name == "red"
    db.grant_allowance(conn, "alice", T0, period="2026-11")
    assert db.balance_micro(conn, "alice") == 70 * M
    assert db.tier_of_user(conn, "alice").name == "standard"


# ---------------------------------------------------------------- rounding
def result(fee: float, rebate: float, surcharge: float = 0.0) -> Settlement:
    return Settlement(zone="saving", fee=fee, rebate=rebate, surcharge=surcharge,
                      net=fee + surcharge - rebate, saved_g=0, saved_unc_g=0)


def test_rounding_directions():
    m = credits.settlement_to_micro(result(1.0000001, 0.5000009, 0.0000001), SettlementParams())
    assert m.fee == 1_000_001  # fee rounds UP
    assert m.surcharge == 1  # surcharge rounds UP
    assert m.rebate == 500_000  # rebate rounds DOWN
    assert m.net == m.fee + m.surcharge - m.rebate


def test_settlement_to_micro_vector_a():
    s = compute_settlement(0.000175, 0.0000169, 700, 70, 80, 8)
    m = credits.settlement_to_micro(s, SettlementParams())
    assert (m.fee, m.rebate, m.surcharge, m.net) == (14 * M, 8_400_000, 0, 5_600_000)


def test_rebate_cap_holds_after_rounding():
    # fee 0.0000021 -> ceil 3 micro (2.1 -> 3). cap = floor(0.6 x 3) = 1.
    # A float rebate of 0.6 x 2.1 = 1.26 micro floors to 1: still within the cap.
    m = credits.settlement_to_micro(result(0.0000021, 0.6 * 0.0000021), SettlementParams(gamma=0.6))
    assert (m.fee, m.rebate) == (3, 1)


def test_property_rounding_never_favours_the_user():
    rng = random.Random(11)
    for _ in range(5000):
        gamma = rng.uniform(0, 0.99)
        fee = rng.choice([rng.uniform(0, 1e-4), rng.uniform(0, 50), rng.uniform(0, 50_000)])
        rebate = rng.uniform(0, gamma * fee)
        surcharge = rng.choice([0.0, rng.uniform(0, 100)])
        m = credits.settlement_to_micro(result(fee, rebate, surcharge), SettlementParams(gamma=gamma))
        assert all(isinstance(v, int) for v in (m.fee, m.rebate, m.surcharge, m.net))
        assert m.fee >= fee * M and m.surcharge >= surcharge * M  # never less than owed
        assert 0 <= m.rebate <= rebate * M  # never more than earned
        assert m.rebate <= gamma * m.fee  # cap still holds
        assert m.net == m.fee + m.surcharge - m.rebate


def test_non_finite_amounts_rejected():
    with pytest.raises(ValueError):
        credits.settlement_to_micro(result(math.nan, 0), SettlementParams())
    with pytest.raises(ValueError):
        credits.to_micro(math.inf)


# ---------------------------------------------------------------- settle_job
E, DE = 0.000175, 0.0000169


def make_job(conn, job_id="j1", user="alice"):
    record_offline_job(conn, job_id=job_id, user_id=user, submitted_at=T0, i_submit=0.0,
                       i_submit_unc=0.0, source="test")


def settle(conn, i_submit, i_submit_unc, i_run, i_run_unc, job_id="j1", now=T0, config=DEFAULT_CONFIG):
    return settle_job(conn, job_id=job_id, user_id="alice", now=now, energy_kwh=E, energy_unc_kwh=DE,
                      i_submit=i_submit, i_submit_unc=i_submit_unc, i_run=i_run, i_run_unc=i_run_unc,
                      config=config)


def test_settle_saving_vector_a(conn):
    db.grant_allowance(conn, "alice", T0, period="2026-11")
    make_job(conn)
    out = settle(conn, 700, 70, 80, 8)
    assert out.zone == "saving" and not out.already_settled
    assert (out.fee_micro, out.rebate_micro, out.surcharge_micro, out.net_micro) == (14 * M, 8_400_000, 0, 5_600_000)
    assert rows(conn)[-2:] == [("fee", -14 * M), ("rebate", 8_400_000)]  # no zero surcharge row
    assert out.balance_micro_after == 100 * M - 5_600_000
    assert out.tier_after == "standard"
    stored = conn.execute("SELECT * FROM settlements WHERE job_id='j1'").fetchone()
    assert stored["zone"] == "saving"
    assert stored["saved_g"] == pytest.approx(0.1085) and stored["saved_unc_g"] == pytest.approx(0.01618, rel=1e-3)
    assert '"k": 1.0' in stored["params_json"]


def test_settle_backfire_with_surcharge(conn):
    make_job(conn)
    cfg = replace(DEFAULT_CONFIG, settlement=SettlementParams(sigma=0.5))
    out = settle(conn, 80, 8, 700, 70, config=cfg)
    assert out.zone == "backfire" and out.rebate_micro == 0
    assert out.fee_micro == 122_500_000
    assert out.surcharge_micro == math.ceil(compute_settlement(E, DE, 80, 8, 700, 70, cfg.settlement).surcharge * M)
    assert rows(conn) == [("fee", -122_500_000), ("surcharge", -out.surcharge_micro)]
    assert out.balance_micro_after < 0 and out.tier_after == "red"  # debt is allowed


def test_settle_dead_zone_writes_fee_only(conn):
    make_job(conn)
    out = settle(conn, 82, 8.2, 80, 8)
    assert out.zone == "dead_zone"
    assert rows(conn) == [("fee", -14 * M)]


def test_settle_is_idempotent(conn):
    make_job(conn)
    first = settle(conn, 700, 70, 80, 8)
    again = settle(conn, 100, 10, 80, 8, now=T0 + DAY)  # different numbers: ignored
    assert again.already_settled and again.zone == first.zone and again.net_micro == first.net_micro
    assert len(rows(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1


def test_settle_applies_decay_first(conn):
    db.grant_allowance(conn, "alice", T0, period="2026-11")
    make_job(conn)
    out = settle(conn, 82, 8.2, 80, 8, now=T0 + 30 * DAY)
    assert out.decay_micro == 50 * M
    assert rows(conn) == [("allowance", 100 * M), ("decay", -50 * M), ("fee", -14 * M)]


def test_settle_is_atomic_on_bad_input(conn):
    db.grant_allowance(conn, "alice", T0, period="2026-11")
    make_job(conn)
    with pytest.raises(ValueError):
        settle(conn, 700, 70, 80, math.nan, now=T0 + 30 * DAY)
    assert rows(conn) == [("allowance", 100 * M)]  # no decay row either: all or nothing
    assert db.last_decay_at(conn, "alice") == T0


def test_settle_requires_the_job_and_its_owner(conn):
    with pytest.raises(ValueError, match="unknown job"):
        settle(conn, 700, 70, 80, 8, job_id="missing")
    db.create_user(conn, "bob", T0)
    make_job(conn, "bobs", user="bob")
    with pytest.raises(ValueError, match="does not belong"):
        settle(conn, 700, 70, 80, 8, job_id="bobs")
    with pytest.raises(ValueError, match="another user"):
        make_job(conn, "bobs", user="alice")


def test_custom_config_is_used(conn):
    cfg = LedgerConfig(half_life_days=10, allowance_credits=40)
    res = db.grant_allowance(conn, "alice", T0, config=cfg)
    assert res.amount_micro == 40 * M
    assert db.apply_decay(conn, "alice", T0 + 10 * DAY, cfg) == 20 * M
