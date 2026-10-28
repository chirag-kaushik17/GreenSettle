"""Facts shared by the poster and the report. Nothing here is typed by hand:

- results come from experiments/results/*.json|csv (written by scripts in this repo),
- placeholder values are read from the code's own config objects,
- anything missing (the real-laptop E1) becomes PENDING.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results"
FIGURES = ROOT / "report" / "figures"
PENDING = "PENDING (run on the laptop)"
NOVELTY = ("ex-post counterfactual settlement with propagated measurement uncertainty and a dead zone")
LIT_NOTE = "Only a limited literature search was done; related work is incomplete."


def _json(name: str):
    path = RESULTS / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def e1() -> dict | None:
    data = _json("e1.json")
    return data if data and not data.get("mock") else None  # mock dry runs are never evidence


def e3() -> dict:
    return _json("e3.json")


def e4() -> dict:
    return _json("e4.json")


def worked() -> dict:
    return _json("worked_examples.json")


def gaming() -> list[dict]:
    path = RESULTS / "gaming_table.csv"
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


def captions() -> dict:
    return json.loads((FIGURES / "captions.json").read_text(encoding="utf-8"))


def headlines() -> dict[str, str]:
    """One sentence per result, every number read from a results file."""
    out: dict[str, str] = {}
    d3 = e3()
    if d3:
        by = {(r["delay_h"], r["forecast_error"], r["defer_z"]): r for r in d3["rows"]}
        r6, r12 = by[(6, 0.1, 1.0)], by[(12, 0.1, 1.0)]
        out["e3"] = (f"E3 (simulated grid and forecast, 10 % forecast error, z = 1): "
                     f"{r6['change_pct']:+.1f} % gCO2e per job with 6 h tolerance (mean wait {r6['mean_wait_h']:.1f} h), "
                     f"{r12['change_pct']:+.1f} % with 12 h; {r6['share_deferrals_backfired']:.0%} and "
                     f"{r12['share_deferrals_backfired']:.0%} of deferrals backfired.")
    d4 = e4()
    if d4:
        by = {(r["k"], r["true_gain"]): r for r in d4["rows"]}
        out["e4"] = (f"E4 (simulated 10 % noise): at k = 1 a rebate went to {by[(1.0, 0.0)]['reward_rate']:.1%} of jobs "
                     f"with no real saving (k = 0: {by[(0.0, 0.0)]['reward_rate']:.1%}), and to "
                     f"{by[(1.0, 0.2)]['reward_rate']:.1%} / {by[(1.0, 0.4)]['reward_rate']:.1%} of jobs with a "
                     f"true 20 % / 40 % saving.")
    rows = gaming()
    if rows:
        ex_post_min = min(float(r["min_net_credits"]) for r in rows if r["rule"].startswith("ex-post"))
        ex_ante_neg = sum(int(r["negative_net_jobs"]) for r in rows if r["rule"] == "ex-ante")
        out["gaming"] = (f"Gaming table (simulated): no ex-post job earned credits (lowest net "
                         f"{ex_post_min:.2f} credits); the simplified ex-ante strawman paid {ex_ante_neg} job(s) to run.")
    d1 = e1()
    if d1:
        parts = [f"{s['workers']} worker(s): CV {s['cv']:.1%}, {s['coverage_1sigma']:.0%} within ±1σ"
                 for s in d1["summary"]]
        out["e1"] = f"E1 (real laptop, {d1['settings']['repeats']} runs × {d1['settings']['seconds']} s): " + "; ".join(parts) + "."
    else:
        out["e1"] = f"E1 repeatability on the real laptop: {PENDING}."
    return out


def placeholders() -> list[tuple[str, str, str]]:
    """(name, value, where) for every placeholder, read from the code."""
    from greensettle.agent.meter import MeterConfig
    from greensettle.intensity.provider import SimulatedCurve
    from greensettle.intensity.timeline import ForecastErrorModel
    from greensettle.ledger.config import DEFAULT_CONFIG

    m, f, c, L = MeterConfig(), ForecastErrorModel(), SimulatedCurve(), DEFAULT_CONFIG
    s = asdict(L.settlement)
    bounds = [t.min_balance_credits for t in L.tiers]
    ranges = []
    for i, t in enumerate(L.tiers):
        lo, hi = bounds[i], bounds[i + 1] if i + 1 < len(bounds) else None
        ranges.append(f"{t.name} " + (f"< {hi:g}" if lo == float("-inf") else f">= {lo:g}" if hi is None else f"{lo:g}-{hi:g}"))
    tiers = (", ".join(ranges) + " credits; priority " + "/".join(str(t.priority) for t in L.tiers)
             + "; max running " + "/".join(str(t.max_concurrent) for t in L.tiers))
    return [
        ("Sensor tolerance", f"{m.sensor_tolerance_fraction:.0%} of package energy", "agent/meter.py MeterConfig"),
        ("Attribution coefficients", f"base {m.attribution_base_fraction:g}, k {m.attribution_k:g}", "agent/meter.py MeterConfig"),
        ("Intensity uncertainty", f"{c.relative_unc:.0%} relative (simulated curve)", "intensity/provider.py SimulatedCurve"),
        ("Forecast error model", f"{f.base_sd:.0%} + {f.sd_per_hour:.0%} per hour of lead, max {f.max_sd:.0%}, bias {f.bias:g}",
         "intensity/timeline.py ForecastErrorModel"),
        ("Tier thresholds", tiers, "ledger/config.py DEFAULT_TIERS"),
        ("p (credits per g CO2e)", format(s["p"], "g"), "settlement/rules.py SettlementParams"),
        ("rho (rebate share)", format(s["rho"], "g"), "settlement/rules.py SettlementParams"),
        ("gamma (rebate cap / fee)", format(s["gamma"], "g"), "settlement/rules.py SettlementParams"),
        ("k (dead-zone width)", format(s["k"], "g"), "settlement/rules.py SettlementParams"),
        ("sigma (backfire surcharge)", format(s["sigma"], "g"), "settlement/rules.py SettlementParams"),
        ("Decay half-life", f"{L.half_life_days:g} days", "ledger/config.py LedgerConfig"),
        ("Monthly allowance", f"{L.allowance_credits:g} credits", "ledger/config.py LedgerConfig"),
    ]


SIMULATED = [
    "the grid carbon-intensity curve (SimulatedCurve, seed 42)",
    "demo overrides (green/dirty) on that curve",
    "the forecast and its error model",
    "the measurement-noise model in the gaming table and E4",
    "job energies in E3, E4 and the gaming table",
]
