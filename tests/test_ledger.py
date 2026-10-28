"""Ledger (SQLite) and micro-credit conversion tests.

These do NOT call compute_settlement, so they pass before it is written.
SettlementResult objects are built by hand where needed.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from greensettle.ledger import credits, db
from greensettle.settlement.rules import SettlementParams, SettlementResult

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.create_user(c, "alice", T0)
    yield c
    c.close()


# ---------------------------------------------------------------- schema
def test_schema_is_created_and_reopening_is_safe(tmp_path):
    path = tmp_path / "sub" / "g.db"
    c = db.connect(path)
    db.create_user(c, "bob", T0)
    c.close()
    c = db.connect(path)  # CREATE ... IF NOT EXISTS: running the schema again is harmless
    assert db.user_exists(c, "bob")
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"users", "ledger", "jobs", "settlements", "ledger_with_balance",
            "ledger_no_update", "ledger_no_delete", "ledger_one_per_period"} <= names


def test_ledger_rejects_update_and_delete(conn):
    db.grant_allowance(conn, "alice", T0, 100, "2026-W45")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger SET amount_micro = 999")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger")
    assert db.balance_micro(conn, "alice") == 100_000_000


def test_kind_check_and_integer_amounts_enforced_by_sqlite(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('alice', 'x', 'gift', 1)")
    with pytest.raises(sqlite3.IntegrityError):  # a float amount would let drift back in
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('alice', 'x', 'fee', 1.5)")


def test_foreign_key_unknown_user_rejected_by_sqlite(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger (user_id, ts, kind, amount_micro) VALUES ('mallory', 'x', 'fee', -1)")


def test_append_entry_validation(conn):
    with pytest.raises(ValueError):
        db.append_entry(conn, "alice", T0, "gift", 1)
    with pytest.raises(TypeError):
        db.append_entry(conn, "alice", T0, "fee", -1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown user"):
        db.append_entry(conn, "nobody", T0, "fee", -1)


# ---------------------------------------------------------------- users, time
def test_create_user_twice_raises(conn):
    with pytest.raises(ValueError, match="already exists"):
        db.create_user(conn, "alice", T0)


def test_naive_datetime_rejected(conn):
    with pytest.raises(ValueError, match="timezone-aware"):
        db.create_user(conn, "carol", datetime(2026, 1, 1))


def test_times_stored_in_utc(conn):
    ist = timezone(timedelta(hours=5, minutes=30))
    db.create_user(conn, "dev", datetime(2026, 1, 1, 10, 0, tzinfo=ist))
    row = conn.execute("SELECT created_at, decayed_through FROM users WHERE user_id='dev'").fetchone()
    assert row["created_at"] == "2026-01-01T04:30:00+00:00"
    assert row["decayed_through"] == row["created_at"]


# ---------------------------------------------------------------- allowance and balance
def test_allowance_is_idempotent_per_period(conn):
    first = db.grant_allowance(conn, "alice", T0, 100, "2026-W45")
    again = db.grant_allowance(conn, "alice", T0 + timedelta(hours=1), 100, "2026-W45")
    nxt = db.grant_allowance(conn, "alice", T0 + timedelta(days=7), 100, "2026-W46")
    assert isinstance(first, int) and again is None and isinstance(nxt, int)
    assert db.balance_micro(conn, "alice") == 200_000_000


def test_allowance_periods_are_per_user(conn):
    db.create_user(conn, "bob", T0)
    assert db.grant_allowance(conn, "alice", T0, 50, "2026-W45") is not None
    assert db.grant_allowance(conn, "bob", T0, 50, "2026-W45") is not None


def test_allowance_validation(conn):
    for bad_amount in (0, -5):
        with pytest.raises(ValueError):
            db.grant_allowance(conn, "alice", T0, bad_amount, "2026-W45")
    with pytest.raises(ValueError):
        db.grant_allowance(conn, "alice", T0, 10, "")
    with pytest.raises(ValueError, match="unknown user"):
        db.grant_allowance(conn, "nobody", T0, 10, "2026-W45")


def test_balance_is_sum_and_view_gives_running_balance(conn):
    db.create_user(conn, "bob", T0)
    db.grant_allowance(conn, "alice", T0, 100, "2026-W45")
    with conn:
        db.append_entry(conn, "alice", T0, "fee", -14_000_000, job_id="j1")
        db.append_entry(conn, "bob", T0, "adjustment", 5_000_000, note="manual")
        db.append_entry(conn, "alice", T0, "rebate", 8_400_000, job_id="j1")
    assert db.balance_micro(conn, "alice") == 100_000_000 - 14_000_000 + 8_400_000
    assert db.balance_micro(conn, "bob") == 5_000_000
    assert db.balance_micro(conn, "nobody") == 0
    running = [r["balance_micro"] for r in conn.execute(
        "SELECT balance_micro FROM ledger_with_balance WHERE user_id='alice' ORDER BY entry_id")]
    assert running == [100_000_000, 86_000_000, 94_400_000]


def test_no_float_drift_over_many_entries(conn):
    # 0.1 credit added 10,000 times: floats would drift, integers do not.
    with conn:
        for _ in range(10_000):
            db.append_entry(conn, "alice", T0, "adjustment", credits.to_micro(0.1))
    assert db.balance_micro(conn, "alice") == 1_000_000_000
    total = 0.0
    for _ in range(10_000):
        total += 0.1
    assert total != 1000.0  # the problem we avoided (total is 1000.0000000001588)


# ---------------------------------------------------------------- micro-credit conversion
def test_to_micro_and_back():
    assert credits.to_micro(14.0) == 14_000_000
    assert credits.to_micro(0.6168) == 616_800
    assert credits.to_micro(1e-7) == 0
    assert credits.to_micro(0.0000006) == 1  # rounds to nearest
    assert credits.from_micro(8_400_000) == pytest.approx(8.4)
    with pytest.raises(ValueError):
        credits.to_micro(float("nan"))


def result(fee: float, rebate: float, surcharge: float = 0.0) -> SettlementResult:
    return SettlementResult(fee=fee, rebate=rebate, surcharge=surcharge, net_cost=fee - rebate + surcharge,
                            saved_g=0, saved_unc_g=0, saved_low_g=0, saved_high_g=0, zone="saving")


def test_settlement_to_micro_vector_a():
    m = credits.settlement_to_micro(result(14.0, 8.4), SettlementParams())
    assert (m.fee, m.rebate, m.surcharge, m.net) == (14_000_000, 8_400_000, 0, 5_600_000)


def test_rounding_cannot_push_rebate_over_the_cap():
    # The cap binds: fee = 0.0000029 credits, rebate = 0.6 x fee = 0.00000174.
    # Rounded separately: fee -> 3 micro, rebate -> 2 micro. But 0.6 x 3 = 1.8,
    # so 2 micro would break "rebate <= gamma x fee". It must be cut to 1.
    m = credits.settlement_to_micro(result(0.0000029, 0.6 * 0.0000029), SettlementParams(rebate_cap=0.6))
    assert (m.fee, m.rebate) == (3, 1)
    assert m.rebate <= 0.6 * m.fee
    assert m.net == 2


def test_property_micro_rounding_keeps_cap_and_identity():
    rng = random.Random(11)
    for _ in range(5000):
        gamma = rng.uniform(0, 0.99)
        fee = rng.choice([rng.uniform(0, 1e-4), rng.uniform(0, 50), rng.uniform(0, 50_000)])
        rebate = rng.uniform(0, gamma * fee)
        surcharge = rng.choice([0.0, rng.uniform(0, 100)])
        m = credits.settlement_to_micro(result(fee, rebate, surcharge), SettlementParams(rebate_cap=gamma))
        assert all(isinstance(v, int) for v in (m.fee, m.rebate, m.surcharge, m.net))
        assert 0 <= m.rebate <= gamma * m.fee
        assert m.net == m.fee - m.rebate + m.surcharge
        if surcharge == 0:
            assert m.net >= (1 - gamma) * m.fee  # follows from the cap, now in integers
        assert abs(m.rebate - rebate * 1e6) <= 1.0  # never off by more than one micro-credit
