"""Gaming table: can users farm credits under each settlement rule?

    python experiments/gaming_table.py [--out experiments/results/gaming_table.csv]

Deterministic (fixed seeds). The parameters are the defaults from
LedgerConfig / SettlementParams; NOTHING was tuned to improve the results.

Four rules, applied to the SAME job streams and the SAME noise draws:
  ex-ante     a SIMPLIFIED STRAWMAN baseline (see EX_ANTE_NOTE)
  ex-post k=0, k=1, k=2   the real compute_settlement() with k changed

Four archetypes, 200 jobs each:
  1 honest_deferrer   defers to the truly greenest 30-min slot within 6 h; honest energy
  2 always_immediate  control: never defers (counterfactual = actual)
  3 junk_farmer       many tiny 1-second jobs, deferred like the honest deferrer
  4 noise_chaser      flat curve (true gain exactly 0); defers 0.5-6 h hoping noise looks like a saving
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow `python experiments/gaming_table.py` from the project root

from greensettle.intensity.provider import IntensityReading, SimulatedCurve  # noqa: E402
from greensettle.intensity.timeline import ActualProvider, ForecastProvider, mean_intensity  # noqa: E402
from greensettle.ledger.config import DEFAULT_CONFIG  # noqa: E402
from greensettle.settlement.rules import compute_settlement  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 11, 1, tzinfo=UTC)
JOBS_PER_ARCHETYPE = 200
WINDOW_H = 6.0
SLOT = timedelta(minutes=30)
REL_E = 0.10  # relative sd of measured energy (about Phase 1's worked example, 59 J / 630 J)
REL_I = 0.10  # relative sd of measured intensity (the simulated curve's own uncertainty)
PARAMS = DEFAULT_CONFIG.settlement  # p, rho, gamma, k, sigma defaults
RULES = ("ex-ante", "ex-post k=0", "ex-post k=1", "ex-post k=2")
ARCHETYPES = ("honest_deferrer", "always_immediate", "junk_farmer", "noise_chaser")

NOISE_NOTE = (
    "Noise model (assumption): measured energy and intensities = true x (1 + N(0, sd)) with "
    f"sd = {REL_E:.0%} for energy and {REL_I:.0%} for intensity, and the reported uncertainty equals the true sd "
    "(honest error bars). Measurements of the submit-time and run-time intensity are independent, except for "
    "always_immediate, where the counterfactual IS the actual run (one shared measurement)."
)
EX_ANTE_NOTE = (
    "Ex-ante baseline (SIMPLIFIED STRAWMAN, not a real scheme): decided at submit time with no measurement. "
    "fee = p x declared_E x forecast(i_run); rebate = rho x p x declared_E x (forecast(i_submit) - forecast(i_run)), "
    "applied as written (a negative value acts as a charge), with no dead zone and no cap. declared_E = true energy."
)


class FlatCurve:
    """A constant grid intensity: the true gain from moving a job is exactly 0."""

    seed = 0
    relative_unc = REL_I

    def __init__(self, g_per_kwh: float = 400.0) -> None:
        self.g = g_per_kwh

    def curve_value(self, timestamp: datetime) -> float:
        return self.g

    def get(self, timestamp: datetime) -> IntensityReading:
        return IntensityReading(self.g, self.g * self.relative_unc, "flat")


@dataclass(frozen=True)
class Job:
    submit: datetime
    run: datetime
    duration: timedelta
    energy_true: float  # kWh


@dataclass(frozen=True)
class Measured:
    energy: float
    energy_unc: float
    i_submit: float
    i_submit_unc: float
    i_run: float
    i_run_unc: float


# --------------------------------------------------------------------------
# Job streams (one seeded RNG per archetype)
# --------------------------------------------------------------------------
def greenest_start(actual: ActualProvider, submit: datetime, duration: timedelta) -> datetime:
    """The truly greenest slot start within the window (ties: earliest)."""
    n = int(WINDOW_H * 3600 // SLOT.total_seconds())
    starts = [submit + i * SLOT for i in range(n + 1)]
    return min(starts, key=lambda s: mean_intensity(actual, s, s + duration)[0])


def job_stream(archetype: str, actual: ActualProvider, seed: int) -> list[Job]:
    rng = random.Random(f"jobs:{archetype}:{seed}")
    jobs = []
    for _ in range(JOBS_PER_ARCHETYPE):
        submit = START + timedelta(minutes=rng.randrange(30 * 24 * 60))  # any minute in 30 days
        if archetype == "junk_farmer":
            duration, energy = timedelta(seconds=1), rng.uniform(2e-6, 4e-6)  # ~1 s at ~10 W
        else:
            duration, energy = timedelta(seconds=60), rng.uniform(1e-4, 1e-3)
        if archetype in ("honest_deferrer", "junk_farmer"):
            run = greenest_start(actual, submit, duration)
        elif archetype == "always_immediate":
            run = submit
        else:  # noise_chaser: a random deferral on a flat curve
            run = submit + rng.randint(1, int(WINDOW_H * 2)) * SLOT
        jobs.append(Job(submit, run, duration, energy))
    return jobs


def measure(job: Job, actual: ActualProvider, rng: random.Random, shared_counterfactual: bool) -> Measured:
    def noisy(true: float, sd: float) -> float:
        return max(0.0, true * (1 + rng.gauss(0.0, sd)))

    i_sub_true = mean_intensity(actual, job.submit, job.submit + job.duration)[0]
    i_run_true = mean_intensity(actual, job.run, job.run + job.duration)[0]
    energy = noisy(job.energy_true, REL_E)
    i_run = noisy(i_run_true, REL_I)
    i_sub = i_run if shared_counterfactual else noisy(i_sub_true, REL_I)
    return Measured(energy, REL_E * job.energy_true, i_sub, REL_I * i_sub_true, i_run, REL_I * i_run_true)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def ex_ante(job: Job, forecast: ForecastProvider) -> tuple[float, float]:
    """(net, rebate) of the simplified strawman."""
    f_sub = forecast.mean(job.submit, job.submit + job.duration, issued_at=job.submit)[0]
    f_run = forecast.mean(job.run, job.run + job.duration, issued_at=job.submit)[0]
    fee = PARAMS.p * job.energy_true * f_run
    rebate = PARAMS.rho * PARAMS.p * job.energy_true * (f_sub - f_run)
    return fee - rebate, rebate


def ex_post(m: Measured, k: float) -> tuple[float, float]:
    s = compute_settlement(m.energy, m.energy_unc, m.i_submit, m.i_submit_unc, m.i_run, m.i_run_unc,
                           replace(PARAMS, k=k))
    return s.net, s.rebate


# --------------------------------------------------------------------------
def run(seed: int = 7) -> list[dict]:
    real_curve = SimulatedCurve(seed=42, tz=UTC)
    flat = FlatCurve()
    results = []
    for archetype in ARCHETYPES:
        curve = flat if archetype == "noise_chaser" else real_curve
        actual, forecast = ActualProvider(curve), ForecastProvider(curve)
        jobs = job_stream(archetype, actual, seed)
        noise_rng = random.Random(f"noise:{archetype}:{seed}")
        measured = [measure(j, actual, noise_rng, archetype == "always_immediate") for j in jobs]
        per_rule = {
            "ex-ante": [ex_ante(j, forecast) for j in jobs],
            "ex-post k=0": [ex_post(m, 0.0) for m in measured],
            "ex-post k=1": [ex_post(m, 1.0) for m in measured],
            "ex-post k=2": [ex_post(m, 2.0) for m in measured],
        }
        for rule in RULES:
            nets = [n for n, _ in per_rule[rule]]
            rebates = [r for _, r in per_rule[rule]]
            results.append({
                "archetype": archetype,
                "rule": rule,
                "jobs": len(nets),
                "mean_net_credits": statistics.fmean(nets),
                "share_rebated": sum(1 for r in rebates if r > 0) / len(rebates),
                "total_rebates_minted": sum(r for r in rebates if r > 0),
                "min_net_credits": min(nets),
                "negative_net_jobs": sum(1 for n in nets if n < 0),
            })
    return results


def markdown(results: list[dict]) -> str:
    lines = [
        "| archetype | rule | mean net / job | share rebated | rebates minted | min net | jobs with net < 0 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['archetype']} | {r['rule']} | {r['mean_net_credits']:.4f} | {r['share_rebated']:.1%} | "
            f"{r['total_rebates_minted']:.4f} | {r['min_net_credits']:.6f} | {r['negative_net_jobs']} |"
        )
    return "\n".join(lines)


def reading(results: list[dict]) -> str:
    """Two to three sentences generated ONLY from the numbers above."""
    get = {(r["archetype"], r["rule"]): r for r in results}
    ex_ante_neg = [r for r in results if r["rule"] == "ex-ante" and r["negative_net_jobs"] > 0]
    if ex_ante_neg:
        detail = ", ".join(f"{r['negative_net_jobs']} {r['archetype']} job(s), lowest {r['min_net_credits']:.4f}"
                           for r in ex_ante_neg)
        s1 = f"Under the simplified ex-ante strawman some jobs were paid to run (net below zero): {detail}."
    else:
        s1 = "Under the simplified ex-ante strawman no job had a negative net in this run."
    ex_post_rows = [r for r in results if r["rule"].startswith("ex-post")]
    worst = min(r["min_net_credits"] for r in ex_post_rows)
    s2 = (f"Across all ex-post runs the lowest net of any job was {worst:.6f} credits, so "
          + ("no job ever earned credits by running." if worst >= 0 else "SOME JOB EARNED CREDITS (see the warning)."))
    nc0, nc2 = get[("noise_chaser", "ex-post k=0")], get[("noise_chaser", "ex-post k=2")]
    s3 = (f"On the flat curve, where the true gain is zero, the noise chaser received a rebate on "
          f"{nc0['share_rebated']:.0%} of jobs with k=0 and on {nc2['share_rebated']:.0%} with k=2.")
    return " ".join((s1, s2, s3))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "experiments" / "results" / "gaming_table.csv"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    results = run(args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    print(f"# Gaming table ({JOBS_PER_ARCHETYPE} jobs per archetype, seed {args.seed})\n")
    print(f"Parameters (defaults, not tuned): p={PARAMS.p:g}, rho={PARAMS.rho:g}, gamma={PARAMS.gamma:g}, "
          f"sigma={PARAMS.sigma:g}; ex-post k varies. Credits are floats here (no ledger rounding).\n")
    print(f"- {NOISE_NOTE}")
    print(f"- {EX_ANTE_NOTE}")
    print("- noise_chaser uses a flat 400 g/kWh curve by definition; the other archetypes use the simulated "
          "curve (seed 42, UTC) and the Phase 3 forecast with its default error model.\n")
    print(markdown(results))
    print(f"\nReading: {reading(results)}")
    print(f"\nCSV saved to {out}")

    bad = [r for r in results if r["rule"].startswith("ex-post") and r["negative_net_jobs"] > 0]
    if bad:
        print("\n!!! WARNING: AN EX-POST RULE PRODUCED A NEGATIVE NET (CREDITS MINTED) !!!", file=sys.stderr)
        for r in bad:
            print(f"!!! {r['archetype']} / {r['rule']}: {r['negative_net_jobs']} jobs, min net "
                  f"{r['min_net_credits']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
