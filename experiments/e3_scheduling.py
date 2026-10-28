"""E3: does scheduling with choose_slot reduce emissions? (SIMULATION)

    python experiments/e3_scheduling.py

7 simulated days, one job every 20 sim minutes (504 jobs). Every job is
planned with the REAL choose_slot (greensettle/scheduler/slots.py), the
real ForecastProvider and the real ActualProvider, for every combination of

    delay tolerance  {0, 2, 6, 12} h
    forecast error   {0 %, 10 %, 20 %}  (constant relative sd, no growth with lead time)
    defer_z          {0, 1, 2}

and compared with running the same job immediately.

Everything here is SIMULATED: the grid curve (SimulatedCurve, seed 42, UTC),
the forecast error (ForecastErrorModel, seed 1234), and a fixed job energy.
There is no measurement noise in E3: it isolates the scheduling decision.

Writes experiments/results/e3.csv and e3.json.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greensettle.intensity.provider import SimulatedCurve  # noqa: E402
from greensettle.intensity.timeline import (  # noqa: E402
    ActualProvider,
    ForecastErrorModel,
    ForecastProvider,
    mean_intensity,
)
from greensettle.scheduler.slots import SchedulerParams, SlotRequest, choose_slot  # noqa: E402

RESULTS = ROOT / "experiments" / "results"
START = datetime(2026, 11, 2, tzinfo=timezone.utc)
DAYS = 7
EVERY = timedelta(minutes=20)
JOB_SIM_S = 15 * 60  # each job spans 15 sim minutes
ENERGY_KWH = 0.0005  # simulated, the same for every job (only the timing differs)
DELAYS_H = (0, 2, 6, 12)
FORECAST_ERRORS = (0.0, 0.10, 0.20)
DEFER_Z = (0.0, 1.0, 2.0)
CURVE_SEED, FORECAST_SEED = 42, 1234


def submit_times(days: int = DAYS) -> list[datetime]:
    n = int(timedelta(days=days) / EVERY)
    return [START + i * EVERY for i in range(n)]


def run(days: int = DAYS, delays=DELAYS_H, errors=FORECAST_ERRORS, zs=DEFER_Z) -> list[dict]:
    curve = SimulatedCurve(seed=CURVE_SEED, tz=timezone.utc)
    actual = ActualProvider(curve)
    times = submit_times(days)
    d = timedelta(seconds=JOB_SIM_S)
    actual_cache: dict[datetime, float] = {}

    def actual_at(start: datetime) -> float:
        if start not in actual_cache:
            actual_cache[start] = mean_intensity(actual, start, start + d)[0]
        return actual_cache[start]

    rows = []
    for err in errors:
        forecast = ForecastProvider(curve, ForecastErrorModel(base_sd=err, sd_per_hour=0.0, max_sd=0.4,
                                                              seed=FORECAST_SEED))
        for delay in delays:
            for z in zs:
                g_sched, g_now, waits, deferred, backfired = [], [], [], 0, 0
                for t0 in times:
                    choice = choose_slot(SlotRequest(t0, float(delay), float(JOB_SIM_S)),
                                         forecast.bound(issued_at=t0), SchedulerParams(defer_z=z))
                    i_now, i_run = actual_at(t0), actual_at(choice.start)
                    g_now.append(ENERGY_KWH * i_now)
                    g_sched.append(ENERGY_KWH * i_run)
                    waits.append((choice.start - t0).total_seconds() / 3600)
                    if choice.deferred:
                        deferred += 1
                        backfired += i_run > i_now
                mean_now, mean_sched = statistics.fmean(g_now), statistics.fmean(g_sched)
                rows.append({
                    "delay_h": delay, "forecast_error": err, "defer_z": z, "jobs": len(times),
                    "mean_g_per_job_immediate": mean_now, "mean_g_per_job_scheduled": mean_sched,
                    "change_pct": 100 * (mean_sched - mean_now) / mean_now,
                    "mean_wait_h": statistics.fmean(waits),
                    "share_deferred": deferred / len(times),
                    "share_deferrals_backfired": (backfired / deferred) if deferred else 0.0,
                })
    return rows


def main() -> int:
    rows = run()
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "e3.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    meta = {
        "experiment": "E3 scheduling benefit", "simulated": True,
        "notes": ["grid curve simulated (SimulatedCurve seed 42, UTC)",
                  "forecast error simulated: constant relative sd per level, seed 1234",
                  f"fixed simulated job energy {ENERGY_KWH} kWh, {JOB_SIM_S // 60} sim minutes per job",
                  "no measurement noise; backfired = actual intensity at the chosen start > at submission"],
        "start": START.isoformat(), "days": DAYS, "every_minutes": EVERY.seconds // 60,
        "rows": rows,
    }
    (RESULTS / "e3.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("| delay h | forecast err | z | change vs immediate | mean wait h | deferred | deferrals backfired |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['delay_h']} | {r['forecast_error']:.0%} | {r['defer_z']:g} | {r['change_pct']:+.1f} % | "
              f"{r['mean_wait_h']:.2f} | {r['share_deferred']:.0%} | {r['share_deferrals_backfired']:.0%} |")
    print(f"\nSIMULATED. Wrote {RESULTS / 'e3.csv'} and e3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
