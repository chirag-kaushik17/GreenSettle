"""GreenSettle Phase 1 command-line interface.

Run from the project root, e.g. in PowerShell:

    python -m greensettle.cli check-sensor
    python -m greensettle.cli dump-powers --save fixtures\\lhm_data_sample.json
    python -m greensettle.cli calibrate-idle --seconds 30
    python -m greensettle.cli run -- python tools/burn.py --seconds 30 --threads 4
    python -m greensettle.cli intensity-force dirty
    python -m greensettle.cli watch
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from greensettle.agent import lhm_client
from greensettle.agent import meter
from greensettle.agent.lhm_client import LhmClient, LhmError
from greensettle.engine.convert import to_gco2e
from greensettle.intensity import provider as intensity
from greensettle.ledger import commands as ledger_commands
from greensettle.ledger import db as ledger_db

log = logging.getLogger("greensettle")


# --------------------------------------------------------------------------
# check-sensor
# --------------------------------------------------------------------------
def cmd_check_sensor(args: argparse.Namespace) -> int:
    client = LhmClient(args.url)
    tree = client.fetch()  # raises LhmUnreachableError with a hint if the server is off
    print(f"OK: LibreHardwareMonitor reachable at {client.url}")
    cpu = lhm_client.find_cpu_node(tree)
    if cpu is None:
        print("ERROR: no CPU hardware node found in data.json")
        return 2
    print(f"CPU node: {cpu.get('Text')}  (HardwareId={cpu.get('HardwareId')})")
    if lhm_client.find_powers_group(cpu) is None:
        print("ERROR: the CPU node has no 'Powers' group")
        return 2

    labels = {
        "ok": "LIVE",
        "dead": "DEAD (reads 0.0 W)",
        "no_reading": "DEAD (no reading)",
        "missing": "MISSING",
    }
    for reading in lhm_client.read_cpu_powers(tree).values():
        watts = "    -   " if reading.watts is None else f"{reading.watts:7.2f} W"
        print(f"  {reading.name:<13} {watts}   {labels[reading.status]}")

    package = lhm_client.read_cpu_powers(tree, (lhm_client.CPU_PACKAGE,))[lhm_client.CPU_PACKAGE]
    if package.status != "ok":
        print("ERROR: CPU Package is not live, so GreenSettle cannot measure energy.")
        return 2
    print("Scope: cpu_package (only CPU Package is used for energy; dead sensors are ignored).")
    return 0


# --------------------------------------------------------------------------
# dump-powers (Step 0 helper: show and optionally save the real structure)
# --------------------------------------------------------------------------
def cmd_dump_powers(args: argparse.Namespace) -> int:
    if args.file:
        tree = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        tree = LhmClient(args.url).fetch()
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved full data.json to {args.save}")
    cpu = lhm_client.find_cpu_node(tree)
    powers = lhm_client.find_powers_group(cpu) if cpu else None
    if powers is None:
        print("No CPU 'Powers' group found.")
        return 2
    print(json.dumps(powers, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# calibrate-idle
# --------------------------------------------------------------------------
def cmd_calibrate_idle(args: argparse.Namespace) -> int:
    client = LhmClient(args.url)
    client.package_watts()  # fail fast with a clear message before waiting N seconds
    config = meter.MeterConfig(poll_interval_s=args.interval)
    path = Path(args.data_dir) / "idle.json"
    print(f"Measuring idle power for {args.seconds:g} s. Please leave the machine alone...")
    idle = meter.calibrate_idle(client.package_watts, args.seconds, config, path)
    print(f"Idle CPU Package: mean {idle.mean_w:.2f} W, std dev {idle.std_w:.2f} W "
          f"({idle.samples} samples over {idle.seconds:.1f} s)")
    print(f"Saved to {path}")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def measurement_record(m: meter.JobMeasurement, reading: intensity.IntensityReading) -> dict:
    """Build the stable JSON record for one job (consumed by later phases)."""
    kwh = meter.joules_to_kwh
    u = m.uncertainty
    record = {
        "job_id": m.job_id,
        "command": subprocess.list2cmdline(m.command),
        "started_at": m.started_at,
        "ended_at": m.ended_at,
        "duration_s": round(m.duration_s, 3),
        "samples": m.sampler.samples,
        "mean_sample_interval_s": round(m.sampler.mean_interval_s, 4),
        "scope": "cpu_package",
        "energy_kwh": {
            "package_total": kwh(m.split.package_total_j),
            "idle_baseline": kwh(m.split.idle_baseline_j),
            "dynamic": kwh(m.split.dynamic_j),
            "job": kwh(m.split.job_j),
        },
        "attribution": {
            "cpu_share": round(m.split.cpu_share, 4),
            "job_cpu_s": round(m.job_cpu_s, 3),
            "system_busy_cpu_s": round(m.system_busy_cpu_s, 3),
        },
        "uncertainty_kwh": {
            "sensor": u.sensor_kwh,
            "sampling": u.sampling_kwh,
            "idle_baseline": u.idle_baseline_kwh,
            "attribution": u.attribution_kwh,
            "combined": u.combined_kwh,
        },
        "intensity": {
            "g_per_kwh": round(reading.g_per_kwh, 2),
            "unc_g_per_kwh": round(reading.unc_g_per_kwh, 2),
            "source": reading.source,
        },
        "gco2e": None,  # filled below
    }
    emissions = to_gco2e(kwh(m.split.job_j), u.combined_kwh, reading.g_per_kwh, reading.unc_g_per_kwh)
    record["gco2e"] = {"value": emissions.value_g, "unc": emissions.unc_g}
    # Optional platform overhead: only adds keys when switched on, so the
    # default schema stays exactly as specified.
    if m.split.platform_overhead_j > 0:
        record["scope"] = "cpu_package+platform_estimate"
        record["energy_kwh"]["platform_overhead"] = kwh(m.split.platform_overhead_j)
        record["uncertainty_kwh"]["platform"] = u.platform_kwh
    return record


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.job_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ERROR: give a command after --, e.g.  run -- python tools/burn.py --seconds 30", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    idle = meter.IdleCalibration.load(data_dir / "idle.json")
    client = LhmClient(args.url)
    client.package_watts()  # fail fast if the sensor is unusable
    config = meter.MeterConfig(
        poll_interval_s=args.interval,
        platform_overhead_w=args.platform_overhead_w,
        platform_overhead_unc_w=args.platform_overhead_unc_w,
    )
    print(f"Running: {subprocess.list2cmdline(command)}")
    m = meter.run_job(command, client.package_watts, idle, config)
    # Intensity at the middle of the job (Phase 1 uses one value per job).
    reading = intensity.make_provider(args.intensity, data_dir).get(m.midpoint_utc)
    record = measurement_record(m, reading)

    out_dir = data_dir / "measurements"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{m.job_id}.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    print(f"Saved to {out_path}")
    if m.exit_code != 0:
        print(f"WARNING: the job exited with code {m.exit_code}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# watch
# --------------------------------------------------------------------------
def cmd_watch(args: argparse.Namespace) -> int:
    """Live view: package watts and the gCO2e per second they would cause.

    Energy in one second = W * 1 s = W joules = W / 3,600,000 kWh.
    The error band uses only the sensor tolerance (there is no integration or
    attribution in a single instantaneous reading) plus the intensity error.
    The demo override file is re-read every second, so `intensity-force` in a
    second terminal takes effect immediately.
    """
    client = LhmClient(args.url)
    config = meter.MeterConfig()
    data_dir = Path(args.data_dir)
    print("Ctrl+C to stop.   (whole CPU package, not per job)")
    print(f"{'time':8}  {'package W':>9}  {'g/kWh':>7}  {'gCO2e/s':>20}  source")
    shown = 0
    try:
        while args.count is None or shown < args.count:
            tick = time.monotonic()
            now = datetime.now(timezone.utc)
            try:
                watts = client.package_watts()
            except LhmError as exc:
                print(f"{now.astimezone():%H:%M:%S}  sensor error: {exc}")
            else:
                reading = intensity.make_provider(args.intensity, data_dir).get(now)
                kwh_per_s = meter.joules_to_kwh(watts)
                e = to_gco2e(kwh_per_s, config.sensor_tolerance_fraction * kwh_per_s,
                             reading.g_per_kwh, reading.unc_g_per_kwh)
                band = f"{e.value_g:.6f} ± {e.unc_g:.6f}"
                print(f"{now.astimezone():%H:%M:%S}  {watts:9.2f}  {reading.g_per_kwh:7.1f}  {band:>20}  {reading.source}")
            shown += 1
            time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        print("stopped")
    return 0


# --------------------------------------------------------------------------
# intensity-force
# --------------------------------------------------------------------------
def cmd_intensity_force(args: argparse.Namespace) -> int:
    state = None if args.state == "off" else args.state
    path = intensity.save_forced_state(Path(args.data_dir), state)
    print(f"Simulated intensity override: {args.state}  (saved in {path})")
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="greensettle", description="GreenSettle: measure (Phase 1) and settle (Phase 2).")
    parser.add_argument(
        "--url",
        default=None,
        help=f"LHM data.json URL (default: ${lhm_client.URL_ENV_VAR} or {lhm_client.DEFAULT_URL})",
    )
    parser.add_argument("--data-dir", default="data", help="where idle.json and measurements go")
    parser.add_argument("--interval", type=float, default=1.0, help="sensor poll interval in seconds")
    parser.add_argument("--intensity", default="simulated", choices=["simulated"],
                        help="carbon intensity provider (default: simulated)")
    parser.add_argument("--db", default=str(ledger_db.DEFAULT_DB_PATH), help="ledger database (Phase 2)")
    parser.add_argument("--clock", default=None,
                        help="Phase 2: simulated 'now' as ISO time (or set $GREENSETTLE_CLOCK)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    ledger_commands.register(sub)

    p = sub.add_parser("check-sensor", help="check the server and list the CPU power sensors")
    p.set_defaults(func=cmd_check_sensor)

    p = sub.add_parser("dump-powers", help="print the CPU 'Powers' subtree of data.json")
    p.add_argument("--file", help="read a saved data.json instead of the live server")
    p.add_argument("--save", help="also save the full data.json to this path")
    p.set_defaults(func=cmd_dump_powers)

    p = sub.add_parser("calibrate-idle", help="measure idle CPU Package power")
    p.add_argument("--seconds", type=float, default=30)
    p.set_defaults(func=cmd_calibrate_idle)

    p = sub.add_parser("run", help="run a command and measure its energy:  run -- <command...>")
    p.add_argument("--platform-overhead-w", type=float, default=0.0,
                   help="OPTIONAL extra platform watts while busy (default 0 = off)")
    p.add_argument("--platform-overhead-unc-w", type=float, default=0.0,
                   help="uncertainty of --platform-overhead-w, in watts")
    p.add_argument("job_command", nargs=argparse.REMAINDER, help="the command to run, after --")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="live package watts and gCO2e/s until Ctrl+C")
    p.add_argument("--count", type=int, default=None, help="stop after N lines (default: run forever)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("intensity-force", help="demo override for the simulated intensity curve")
    p.add_argument("state", choices=["green", "dirty", "off"])
    p.set_defaults(func=cmd_intensity_force)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args.url = lhm_client.resolve_url(args.url)
    try:
        return args.func(args)
    except (LhmError, FileNotFoundError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
