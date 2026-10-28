"""The e2e demo script, run with a STAND-IN choose_slot (job A +1 h, job B +2 h)
so steps 3-5 are checked before the student's choose_slot exists. Takes ~30 s."""

from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

from greensettle.scheduler.slots import SlotChoice

SPEC = importlib.util.spec_from_file_location(
    "e2e_demo", Path(__file__).resolve().parent.parent / "experiments" / "e2e_demo.py")
e2e_demo = importlib.util.module_from_spec(SPEC)
sys.modules["e2e_demo"] = e2e_demo
SPEC.loader.exec_module(e2e_demo)


def scripted_choose(request, forecast_mean, params) -> SlotChoice:
    """Stand-in: a 3 h window defers 1 h, a 6 h window defers 2 h. Not the slot rule."""
    t0 = request.submit_time
    start = t0 + timedelta(hours=1 if request.max_delay_h <= 3 else 2)
    d = timedelta(seconds=request.est_duration_s)
    now_mean, _ = forecast_mean(t0, t0 + d)
    mean, unc = forecast_mean(start, start + d)
    return SlotChoice(start, mean, unc, now_mean, now_mean - mean, True, "deferred", ())


def test_demo_runs_to_the_end_with_a_backfire(capsys):
    job = {"job_type": "burn", "params": {"seconds": 2, "workers": 1}, "est_duration_s": 3}
    demo = e2e_demo.Demo(600.0, mock_lhm=True, lhm_url=None, choose=scripted_choose, job=job)
    demo.start()
    try:
        assert demo.run() == 0
    finally:
        demo.stop()
    out = capsys.readouterr().out
    assert "STOP" not in out
    section4 = out.split("=== 4.")[1].split("=== 5.")[0]
    assert "zone BACKFIRE" in section4
    assert "dispatch order at the start" in out and "dispatch order now" in out
    assert "Demo finished." in out
