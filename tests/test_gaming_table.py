"""The gaming-table experiment is deterministic and no ex-post job earns credits."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "gaming_table", Path(__file__).resolve().parent.parent / "experiments" / "gaming_table.py")
gaming_table = importlib.util.module_from_spec(SPEC)
sys.modules["gaming_table"] = gaming_table  # dataclasses need their module registered
SPEC.loader.exec_module(gaming_table)


def test_deterministic_and_complete():
    a, b = gaming_table.run(seed=7), gaming_table.run(seed=7)
    assert a == b
    assert {(r["archetype"], r["rule"]) for r in a} == {
        (arch, rule) for arch in gaming_table.ARCHETYPES for rule in gaming_table.RULES}
    assert all(r["jobs"] == 200 for r in a)


def test_no_ex_post_net_is_negative_and_control_gets_no_rebate():
    results = gaming_table.run(seed=7)
    for r in results:
        if r["rule"].startswith("ex-post"):
            assert r["negative_net_jobs"] == 0 and r["min_net_credits"] > 0, r
        if r["archetype"] == "always_immediate":
            assert r["share_rebated"] == 0.0, r


def test_wider_dead_zone_pays_fewer_rebates_on_pure_noise():
    get = {(r["archetype"], r["rule"]): r for r in gaming_table.run(seed=7)}
    shares = [get[("noise_chaser", f"ex-post k={k}")]["share_rebated"] for k in (0, 1, 2)]
    assert shares[0] > shares[1] > shares[2]
