"""Phase 2 CLI commands, end to end on a temporary database (no server)."""

from __future__ import annotations

import json
import re

from greensettle import cli
from greensettle.ledger import db

NOW = "2026-11-02T08:00:00+00:00"
MEASUREMENT = {"job_id": "job-cli-1", "duration_s": 30.0,
               "energy_kwh": {"job": 0.000275}, "uncertainty_kwh": {"combined": 0.0000298}}


def run(capsys, tmp_path, *argv, now=NOW):
    rc = cli.main(["--db", str(tmp_path / "g.db"), "--clock", now, *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_user_allowance_balance_and_ledger(tmp_path, capsys):
    assert run(capsys, tmp_path, "user-add", "alice")[0] == 0
    rc, out, _ = run(capsys, tmp_path, "allowance", "alice")
    assert rc == 0 and "granted 100 credits for 2026-11" in out and "tier green" in out
    rc, out, _ = run(capsys, tmp_path, "allowance", "alice")
    assert rc == 0 and "already granted for 2026-11" in out
    rc, out, _ = run(capsys, tmp_path, "balance", "alice", now="2026-12-02T08:00:00+00:00")
    assert "100.000000 credits" in out and "tier        green" in out
    assert re.search(r"pending\s+-50\.\d+ decay", out)  # 30 days = one half-life (pending, not yet applied)
    rc, out, _ = run(capsys, tmp_path, "ledger", "alice", "--limit", "5")
    assert rc == 0 and "allowance" in out and "2026-11" in out


def test_settle_command_and_idempotency(tmp_path, capsys):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(MEASUREMENT))
    run(capsys, tmp_path, "user-add", "alice")
    run(capsys, tmp_path, "allowance", "alice")
    args = ("settle", str(path), "--user", "alice",
            "--submit-time", "2026-11-02T19:00:00+00:00", "--run-time", "2026-11-02T13:00:00+00:00")
    rc, out, _ = run(capsys, tmp_path, *args)
    assert rc == 0
    for label in ("zone", "fee", "rebate", "surcharge", "net", "balance"):
        assert re.search(rf"^{label}\s", out, re.M), label

    conn = db.connect(tmp_path / "g.db")
    stored = conn.execute("SELECT * FROM settlements WHERE job_id = 'job-cli-1'").fetchone()
    assert stored is not None and stored["zone"] in ("saving", "dead_zone", "backfire")
    balance_after = db.balance_micro(conn, "alice")
    assert balance_after == 100_000_000 - stored["net_micro"]

    rc, out, _ = run(capsys, tmp_path, *args)
    assert rc == 0 and "already settled" in out
    assert db.balance_micro(conn, "alice") == balance_after  # nothing written the second time


def test_cli_errors_are_reported_not_crashed(tmp_path, capsys):
    run(capsys, tmp_path, "user-add", "alice")
    rc, _, err = run(capsys, tmp_path, "allowance", "alice", "--period", "2026-W45")
    assert rc == 2 and "YYYY-MM" in err
    rc, _, err = run(capsys, tmp_path, "user-add", "alice")
    assert rc == 2 and "already exists" in err
    rc, _, err = run(capsys, tmp_path, "balance", "nobody")
    assert rc == 2 and "unknown user" in err
    rc, _, err = run(capsys, tmp_path, "user-add", "bob", now="2026-11-02T08:00:00")
    assert rc == 2 and "time zone" in err


def test_clock_from_environment_and_ledger_newest_first(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GREENSETTLE_CLOCK", "2026-10-15T08:00:00+00:00")
    db_arg = ["--db", str(tmp_path / "g.db")]
    assert cli.main([*db_arg, "user-add", "alice"]) == 0
    assert cli.main([*db_arg, "allowance", "alice"]) == 0  # October, from the env clock
    assert cli.main([*db_arg, "--clock", "2026-11-15T08:00:00+00:00", "allowance", "alice"]) == 0
    capsys.readouterr()
    assert cli.main([*db_arg, "ledger", "alice"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines()[1:] if l.strip()]
    kinds = [l.split()[2] for l in lines]
    assert kinds == ["allowance", "decay", "allowance"]  # newest first
    assert "2026-11" in lines[0] and "2026-10" in lines[-1]
