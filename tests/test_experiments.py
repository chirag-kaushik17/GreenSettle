"""Phase 4 experiments: quick, reduced-size checks (the full runs are scripts)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent / "experiments"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need their module registered
    spec.loader.exec_module(module)
    return module


def test_e3_small_run_is_deterministic_and_sane():
    e3 = load("e3_scheduling")
    a = e3.run(days=1, delays=(0, 6), errors=(0.0, 0.2), zs=(0.0, 2.0))
    assert a == e3.run(days=1, delays=(0, 6), errors=(0.0, 0.2), zs=(0.0, 2.0))
    for r in a:
        assert r["jobs"] == 72
        if r["delay_h"] == 0:  # no window: identical to running immediately
            assert r["change_pct"] == 0 and r["share_deferred"] == 0
        if r["forecast_error"] == 0.0:  # a perfect forecast never backfires
            assert r["share_deferrals_backfired"] == 0
    perfect = [r for r in a if r["delay_h"] == 6 and r["forecast_error"] == 0.0]
    assert all(r["change_pct"] < 0 for r in perfect)
