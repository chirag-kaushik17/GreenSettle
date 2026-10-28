"""E4: how the dead-zone width k trades false rewards against true rewards (SIMULATION)

    python experiments/e4_deadzone.py

For k = 0, 0.5, ..., 3 the REAL compute_settlement settles the same 2000
simulated jobs in three scenarios:

    true gain  0 %   i_run = i_submit         -> any rebate is a FALSE reward
    true gain 20 %   i_run = 0.8 x i_submit   -> a rebate is deserved
    true gain 40 %   i_run = 0.6 x i_submit

Noise model (SIMULATED, the same as experiments/gaming_table.py): measured
energy and intensities = true x (1 + N(0, sd)), sd = 10 %, and the reported
uncertainty equals the true sd (honest error bars). The submit-time and
run-time intensities are measured independently. i_submit is a flat
simulated 400 g/kWh. Other parameters are the defaults (placeholders).

Writes experiments/results/e4.csv and e4.json.
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from gaming_table import PARAMS, REL_E, REL_I  # noqa: E402  (shared noise model and parameters)

from greensettle.settlement.rules import compute_settlement  # noqa: E402

RESULTS = ROOT / "experiments" / "results"
KS = tuple(i / 2 for i in range(7))  # 0, 0.5, ..., 3
GAINS = (0.0, 0.20, 0.40)
JOBS = 2000
I_SUBMIT_TRUE = 400.0
SEED = 11


def draws(n: int, seed: int) -> list[tuple[float, float, float, float]]:
    """(true energy, energy noise, submit noise, run noise) per job: shared by every k and gain."""
    rng = random.Random(seed)
    return [(rng.uniform(1e-4, 1e-3), rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(n)]


def run(jobs: int = JOBS, ks=KS, gains=GAINS, seed: int = SEED) -> list[dict]:
    jobs_draws = draws(jobs, seed)
    rows = []
    for gain in gains:
        i_run_true = I_SUBMIT_TRUE * (1 - gain)
        measured = []
        for e_true, ze, zs, zr in jobs_draws:
            measured.append((
                max(0.0, e_true * (1 + REL_E * ze)), REL_E * e_true,
                max(0.0, I_SUBMIT_TRUE * (1 + REL_I * zs)), REL_I * I_SUBMIT_TRUE,
                max(0.0, i_run_true * (1 + REL_I * zr)), REL_I * i_run_true,
            ))
        for k in ks:
            results = [compute_settlement(*m, replace(PARAMS, k=k)) for m in measured]
            rebates = [s.rebate for s in results]
            rows.append({
                "k": k, "true_gain": gain, "jobs": jobs,
                "reward_rate": sum(1 for r in rebates if r > 0) / jobs,
                "credits_minted": sum(rebates),
                "mean_net": statistics.fmean(s.net for s in results),
                "min_net": min(s.net for s in results),
            })
    return rows


def main() -> int:
    rows = run()
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "e4.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    meta = {
        "experiment": "E4 dead-zone sweep", "simulated": True,
        "notes": [f"noise model simulated: x (1 + N(0, sd)), sd {REL_E:.0%} energy, {REL_I:.0%} intensity, honest error bars",
                  f"i_submit flat simulated {I_SUBMIT_TRUE:g} g/kWh; {JOBS} jobs per scenario, seed {SEED}",
                  "true gain 0: every rebate is a false reward",
                  f"parameters (placeholders): p={PARAMS.p:g}, rho={PARAMS.rho:g}, gamma={PARAMS.gamma:g}, sigma={PARAMS.sigma:g}"],
        "rows": rows,
    }
    (RESULTS / "e4.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("| k | false-reward rate (gain 0) | reward rate (gain 20 %) | reward rate (gain 40 %) | "
          "credits minted (0 / 20 / 40 %) |")
    print("|---:|---:|---:|---:|---:|")
    by = {(r["k"], r["true_gain"]): r for r in rows}
    for k in KS:
        r0, r20, r40 = (by[(k, g)] for g in GAINS)
        print(f"| {k:g} | {r0['reward_rate']:.1%} | {r20['reward_rate']:.1%} | {r40['reward_rate']:.1%} | "
              f"{r0['credits_minted']:.1f} / {r20['credits_minted']:.1f} / {r40['credits_minted']:.1f} |")
    worst = min(r["min_net"] for r in rows)
    print(f"\nLowest net of any job: {worst:.6f} credits. SIMULATED. Wrote {RESULTS / 'e4.csv'} and e4.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
