"""CLI commands end to end against the mock server."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from greensettle import cli
from greensettle.agent import meter

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_SCHEMA = {
    "job_id", "command", "started_at", "ended_at", "duration_s", "samples",
    "mean_sample_interval_s", "scope", "energy_kwh", "attribution",
    "uncertainty_kwh", "intensity", "gco2e",
}


def test_check_sensor_flags_dead_sensors(mock_lhm, capsys):
    assert cli.main(["--url", mock_lhm.url, "check-sensor"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"CPU Package\s+4\.50 W\s+LIVE", out)
    assert re.search(r"CPU Cores\s+[\d.]+ W\s+LIVE", out)
    assert re.search(r"CPU Memory .*DEAD", out)
    assert re.search(r"CPU Platform .*DEAD", out)


def test_check_sensor_unreachable(capsys):
    assert cli.main(["--url", "http://127.0.0.1:9/data.json", "check-sensor"]) == 2
    assert "Remote Web Server" in capsys.readouterr().err


def test_run_writes_stable_schema(mock_lhm, tmp_path, capsys):
    mock_lhm.watts_fn = lambda: 25.0
    meter.IdleCalibration(4.5, 0.3, 30, 30.0, "2026-01-01T00:00:00+00:00").save(tmp_path / "idle.json")
    rc = cli.main([
        "--url", mock_lhm.url, "--data-dir", str(tmp_path), "--interval", "0.2",
        "run", "--", sys.executable, str(ROOT / "tools" / "burn.py"), "--seconds", "1", "--threads", "1",
    ])
    assert rc == 0
    files = list((tmp_path / "measurements").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert set(record) == EXPECTED_SCHEMA
    assert record["scope"] == "cpu_package"
    assert set(record["energy_kwh"]) == {"package_total", "idle_baseline", "dynamic", "job"}
    assert set(record["uncertainty_kwh"]) == {"sensor", "sampling", "idle_baseline", "attribution", "combined"}
    assert record["energy_kwh"]["job"] > 0
    assert 0 <= record["attribution"]["cpu_share"] <= 1
    assert record["uncertainty_kwh"]["combined"] > 0
    assert record["gco2e"]["value"] > 0 and record["gco2e"]["unc"] > 0
    assert record["job_id"] == files[0].stem


def test_run_without_calibration_explains(mock_lhm, tmp_path, capsys):
    rc = cli.main(["--url", mock_lhm.url, "--data-dir", str(tmp_path), "run", "--", sys.executable, "-c", "pass"])
    assert rc == 2
    assert "calibrate-idle" in capsys.readouterr().err


def _watch_g_per_s(url: str, data_dir: Path, capsys) -> float:
    cli.main(["--url", url, "--data-dir", str(data_dir), "watch", "--count", "1"])
    line = capsys.readouterr().out.strip().splitlines()[-1]
    return float(line.split()[3])  # columns: time, W, g/kWh, gCO2e/s, ±, unc, source


def test_watch_dirty_is_higher_than_green_for_same_load(mock_lhm, tmp_path, capsys):
    mock_lhm.set_package_watts(30.0)
    cli.main(["--data-dir", str(tmp_path), "intensity-force", "dirty"])
    dirty = _watch_g_per_s(mock_lhm.url, tmp_path, capsys)
    cli.main(["--data-dir", str(tmp_path), "intensity-force", "green"])
    green = _watch_g_per_s(mock_lhm.url, tmp_path, capsys)
    assert dirty > green > 0
    # 30 W for 1 s at 700 g/kWh = 30 / 3.6e6 * 700 g
    assert dirty == round(30 / 3.6e6 * 700, 6)
