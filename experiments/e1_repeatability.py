"""E1: how repeatable is the per-job energy measurement? (REAL LAPTOP)

    python experiments/e1_repeatability.py --repeats 6 --seconds 20 --workers 1,4
    python experiments/e1_repeatability.py --mock-lhm --repeats 2 --seconds 2 --cooldown 0 --rest 0   # dry run

Runs the catalog `burn` job through the Phase 1 meter (run_job) `repeats`
times for each workers setting, and reports per setting:
  mean, sd and coefficient of variation (sd / mean) of E_job, and
  coverage = the fraction of runs whose E_job lies within +/- 1 REPORTED
  sigma (the run's own combined uncertainty) of the setting's mean.

Defaults take about 6 minutes: 2 settings x 6 runs x 20 s, a 30 s cooldown
before each setting and 10 s rest between runs (the laptop reached 103 C under
load). Each run is capped at 60 s by the job catalog.

Needs LibreHardwareMonitor running and data/idle.json (calibrate-idle).
Real runs write experiments/results/e1.csv and e1.json. A --mock-lhm dry run
writes e1_mock.* instead, so mock numbers can never be mistaken for evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greensettle.agent import lhm_client, meter  # noqa: E402
from greensettle.agent.worker import WorkerError, load_idle  # noqa: E402
from greensettle.jobs import catalog  # noqa: E402

RESULTS = ROOT / "experiments" / "results"
MOCK_WARNING = "mock results are NOT evidence: the power comes from a fake sensor that follows CPU load"


def summarise(workers: int, runs: list[dict]) -> dict:
    energies = [r["energy_kwh_job"] for r in runs]
    mean = statistics.fmean(energies)
    sd = statistics.stdev(energies) if len(energies) >= 2 else 0.0
    within = sum(1 for r in runs if abs(r["energy_kwh_job"] - mean) <= r["energy_unc_kwh"])
    return {"workers": workers, "runs": len(runs), "mean_kwh": mean, "sd_kwh": sd,
            "cv": sd / mean if mean > 0 else float("nan"), "coverage_1sigma": within / len(runs),
            "mean_reported_unc_kwh": statistics.fmean(r["energy_unc_kwh"] for r in runs)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--workers", default="1,4", help="comma-separated workers settings")
    parser.add_argument("--cooldown", type=float, default=30.0, help="seconds to rest before each setting")
    parser.add_argument("--rest", type=float, default=10.0, help="seconds to rest between runs")
    parser.add_argument("--mock-lhm", action="store_true", help="dry run with the fake sensor (NOT evidence)")
    parser.add_argument("--lhm-url", default=None)
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="where idle.json is")
    parser.add_argument("--out-dir", default=str(RESULTS))
    args = parser.parse_args(argv)

    settings = [int(w) for w in args.workers.split(",") if w.strip()]
    argvs = {w: catalog.build_argv("burn", {"seconds": args.seconds, "workers": w}) for w in settings}  # 60 s cap

    mock = None
    if args.mock_lhm:
        print(f"*** {MOCK_WARNING} ***")
        from tests.mock_lhm import MockLhmServer, follow_load_watts
        mock = MockLhmServer(watts_fn=follow_load_watts()).start()
        client = lhm_client.LhmClient(mock.url)
        idle = meter.IdleCalibration(4.5, 0.3, 30, 30.0, datetime.now(timezone.utc).isoformat())  # the mock's idle
    else:
        client = lhm_client.LhmClient(lhm_client.resolve_url(args.lhm_url))
        try:
            idle = load_idle(Path(args.data_dir))
            client.package_watts()
        except (WorkerError, lhm_client.LhmError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    total = len(settings) * args.repeats * (args.seconds + args.rest) + len(settings) * args.cooldown
    print(f"E1: {args.repeats} runs x {settings} workers x {args.seconds} s, about {total / 60:.1f} min")
    runs: list[dict] = []
    try:
        for w in settings:
            print(f"cooldown {args.cooldown:g} s before workers={w} ...", flush=True)
            time.sleep(args.cooldown)
            for i in range(args.repeats):
                m = meter.run_job(argvs[w], client.package_watts, idle)
                row = {"workers": w, "repeat": i + 1, "exit_code": m.exit_code,
                       "energy_kwh_job": meter.joules_to_kwh(m.split.job_j),
                       "energy_unc_kwh": m.uncertainty.combined_kwh, "duration_s": m.duration_s,
                       "cpu_share": m.split.cpu_share, "job_cpu_s": m.job_cpu_s, "samples": m.sampler.samples}
                runs.append(row)
                print(f"  workers={w} run {i + 1}: E_job {row['energy_kwh_job'] * 3.6e6:.1f} J "
                      f"± {row['energy_unc_kwh'] * 3.6e6:.1f} J, cpu_share {row['cpu_share']:.3f}", flush=True)
                if i + 1 < args.repeats:
                    time.sleep(args.rest)
    finally:
        if mock:
            mock.stop()

    failed = [r for r in runs if r["exit_code"] != 0]
    if failed:
        print(f"ERROR: {len(failed)} run(s) failed; not writing results", file=sys.stderr)
        return 1
    summary = [summarise(w, [r for r in runs if r["workers"] == w]) for w in settings]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = "e1_mock" if mock else "e1"
    with (out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(runs[0]))
        writer.writeheader()
        writer.writerows(runs)
    meta = {
        "experiment": "E1 repeatability", "mock": bool(mock), "simulated": False,
        "warning": MOCK_WARNING if mock else None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "lhm_url": client.url, "idle": {"mean_w": idle.mean_w, "std_w": idle.std_w, "measured_at": idle.measured_at},
        "settings": {"repeats": args.repeats, "seconds": args.seconds, "workers": settings,
                     "cooldown_s": args.cooldown, "rest_s": args.rest},
        "summary": summary, "runs": runs,
    }
    (out / f"{stem}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("\n| workers | runs | mean E_job (J) | sd (J) | CV | coverage ±1σ |")
    print("|---:|---:|---:|---:|---:|---:|")
    for s in summary:
        print(f"| {s['workers']} | {s['runs']} | {s['mean_kwh'] * 3.6e6:.1f} | {s['sd_kwh'] * 3.6e6:.1f} | "
              f"{s['cv']:.1%} | {s['coverage_1sigma']:.0%} |")
    print(f"\nWrote {out / (stem + '.csv')} and {stem}.json")
    if mock:
        print(f"*** {MOCK_WARNING} ***")
    return 0


if __name__ == "__main__":
    sys.exit(main())
