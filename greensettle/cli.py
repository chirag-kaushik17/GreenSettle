"""GreenSettle Phase 1 command-line interface.

Run from the project root, e.g. in PowerShell:

    python -m greensettle.cli check-sensor
    python -m greensettle.cli dump-powers --save fixtures\\lhm_data_sample.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from greensettle.agent import lhm_client
from greensettle.agent import meter
from greensettle.agent.lhm_client import LhmClient, LhmError

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
# Argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="greensettle", description="GreenSettle Phase 1: measure.")
    parser.add_argument(
        "--url",
        default=None,
        help=f"LHM data.json URL (default: ${lhm_client.URL_ENV_VAR} or {lhm_client.DEFAULT_URL})",
    )
    parser.add_argument("--data-dir", default="data", help="where idle.json and measurements go")
    parser.add_argument("--interval", type=float, default=1.0, help="sensor poll interval in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-sensor", help="check the server and list the CPU power sensors")
    p.set_defaults(func=cmd_check_sensor)

    p = sub.add_parser("dump-powers", help="print the CPU 'Powers' subtree of data.json")
    p.add_argument("--file", help="read a saved data.json instead of the live server")
    p.add_argument("--save", help="also save the full data.json to this path")
    p.set_defaults(func=cmd_dump_powers)

    p = sub.add_parser("calibrate-idle", help="measure idle CPU Package power")
    p.add_argument("--seconds", type=float, default=30)
    p.set_defaults(func=cmd_calibrate_idle)

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
    except LhmError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
