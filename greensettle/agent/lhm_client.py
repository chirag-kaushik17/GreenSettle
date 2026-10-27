"""Client for LibreHardwareMonitor's Remote Web Server (data.json).

LibreHardwareMonitor (LHM) serves its full sensor tree as JSON, by default at
http://127.0.0.1:8085/data.json. The tree looks like this (abridged):

    {"Text": "Sensor", "Children": [                     <- root
      {"Text": "MY-LAPTOP", "Children": [                <- computer
        {"Text": "Intel Core Ultra ...",                 <- hardware node
         "HardwareId": "/intelcpu/0", "Children": [
          {"Text": "Powers", "Children": [               <- sensor group
            {"Text": "CPU Package", "Type": "Power",     <- sensor
             "SensorId": "/intelcpu/0/power/0",
             "Value": "4.5 W", "RawValue": 4.5}, ...]}]}]}]}

This module only does three things:
  1. fetch the JSON over HTTP (fetch_tree),
  2. find the CPU's "Powers" group in the tree (find_cpu_node, find_powers_group),
  3. turn each power sensor into a number of watts (parse_watts).

Nothing here talks to Windows or changes LHM settings: it is read-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8085/data.json"
URL_ENV_VAR = "GREENSETTLE_LHM_URL"
DEFAULT_TIMEOUT_S = 2.0

# The four CPU power sensors we care about, by their display name in LHM.
CPU_PACKAGE = "CPU Package"
CPU_POWER_SENSORS: tuple[str, ...] = ("CPU Package", "CPU Cores", "CPU Memory", "CPU Platform")

ENABLE_SERVER_HINT = (
    "In LibreHardwareMonitor (running as administrator) open "
    "Options -> Remote Web Server -> Run, and check the port is 8085. "
    "Then open the URL in a browser to confirm it returns JSON."
)

Node = dict[str, Any]


# --------------------------------------------------------------------------
# Errors: one class per situation so the CLI can print a helpful message.
# --------------------------------------------------------------------------
class LhmError(RuntimeError):
    """Base class for every LibreHardwareMonitor-related problem."""


class LhmUnreachableError(LhmError):
    """The web server did not answer (not running, wrong port, firewall...)."""


class SensorMissingError(LhmError):
    """The tree was fetched, but the CPU node or a sensor is not in it."""


class SensorDeadError(LhmError):
    """The sensor exists but reports no usable value (null or always 0 W)."""


@dataclass(frozen=True)
class SensorReading:
    """One power sensor at one moment.

    status is one of:
      "ok"         - a positive number of watts
      "dead"       - present but reads exactly 0.0 W (on this laptop, CPU Memory
                     and CPU Platform always do this)
      "no_reading" - present but RawValue is null and Value is "-" or empty
      "missing"    - not present in the tree at all
    """

    name: str
    watts: float | None
    status: str


def resolve_url(cli_url: str | None = None) -> str:
    """URL priority: CLI flag > environment variable > default."""
    return cli_url or os.environ.get(URL_ENV_VAR) or DEFAULT_URL


# --------------------------------------------------------------------------
# 1. Fetching
# --------------------------------------------------------------------------
def fetch_tree(url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT_S) -> Node:
    """Download and decode data.json. Raises LhmUnreachableError on any network problem."""
    # The sensor server is local, so we deliberately bypass any HTTP proxy that
    # might be configured in environment variables (an empty ProxyHandler).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise LhmUnreachableError(
            f"Could not reach LibreHardwareMonitor at {url} ({exc}). {ENABLE_SERVER_HINT}"
        ) from exc
    try:
        tree = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LhmError(f"{url} answered, but not with valid JSON: {exc}") from exc
    if not isinstance(tree, dict):
        raise LhmError(f"{url} returned JSON, but the top level is not an object.")
    return tree


# --------------------------------------------------------------------------
# 2. Walking the tree
# --------------------------------------------------------------------------
def iter_nodes(node: Node) -> Iterator[Node]:
    """Yield a node and all of its descendants (depth-first)."""
    yield node
    for child in node.get("Children") or []:
        if isinstance(child, dict):
            yield from iter_nodes(child)


def find_cpu_node(tree: Node) -> Node | None:
    """Find the CPU hardware node.

    LHM gives every hardware node a HardwareId such as "/intelcpu/0" or
    "/amdcpu/0". We take the first node whose HardwareId contains "cpu".
    As a fallback (in case the field is absent) we use the CPU icon name.
    """
    for node in iter_nodes(tree):
        if "cpu" in str(node.get("HardwareId", "")).lower():
            return node
    for node in iter_nodes(tree):
        if "cpu.png" in str(node.get("ImageURL", "")).lower():
            return node
    return None


def find_powers_group(cpu_node: Node) -> Node | None:
    """Return the direct child of the CPU node whose Text is "Powers"."""
    for child in cpu_node.get("Children") or []:
        if isinstance(child, dict) and child.get("Text") == "Powers":
            return child
    return None


# --------------------------------------------------------------------------
# 3. Turning a sensor node into watts
# --------------------------------------------------------------------------
_NUMBER_CHARS = re.compile(r"[^0-9,.\-+]")


def parse_number(text: Any) -> float | None:
    """Parse a formatted LHM value such as "4.5 W", "4,5 W", "-" or "".

    Steps: drop the unit and spaces, then work out the decimal mark.
    LHM formats Value with the Windows locale, so a German locale gives "4,5 W".
    If both "," and "." appear, the LAST one is the decimal mark and the other
    is a thousands separator ("1.234,5" or "1,234.5").
    Returns None when there is no number ("-", "", None, garbage).
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    cleaned = _NUMBER_CHARS.sub("", str(text))
    if cleaned in ("", "-", "+"):
        return None
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal, ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_watts(sensor: Node) -> float | None:
    """Watts for one sensor node: prefer RawValue, fall back to formatted Value."""
    raw = sensor.get("RawValue")
    if raw is not None:
        value = parse_number(raw)  # handles a number or a numeric string
        if value is not None:
            return value
    return parse_number(sensor.get("Value"))


def read_cpu_powers(tree: Node, names: tuple[str, ...] = CPU_POWER_SENSORS) -> dict[str, SensorReading]:
    """Read the named CPU power sensors from an already-fetched tree.

    Never raises for missing or dead sensors; that is reported in .status so
    `check-sensor` can show all four at once.
    """
    cpu = find_cpu_node(tree)
    powers = find_powers_group(cpu) if cpu else None
    by_name: dict[str, Node] = {}
    if powers:
        for sensor in powers.get("Children") or []:
            if isinstance(sensor, dict) and "Text" in sensor:
                by_name[sensor["Text"]] = sensor

    readings: dict[str, SensorReading] = {}
    for name in names:
        node = by_name.get(name)
        if node is None:
            readings[name] = SensorReading(name, None, "missing")
            continue
        watts = parse_watts(node)
        if watts is None:
            status = "no_reading"
        elif watts == 0.0:
            status = "dead"
        else:
            status = "ok"
        readings[name] = SensorReading(name, watts, status)
    return readings


def package_watts_from_tree(tree: Node) -> float:
    """The one number the meter needs: CPU Package watts. Raises if unusable."""
    if find_cpu_node(tree) is None:
        raise SensorMissingError("No CPU hardware node found in data.json (no HardwareId containing 'cpu').")
    reading = read_cpu_powers(tree, (CPU_PACKAGE,))[CPU_PACKAGE]
    if reading.status == "missing":
        raise SensorMissingError(
            f"Sensor '{CPU_PACKAGE}' not found under the CPU's 'Powers' group. "
            "Run `check-sensor` or `dump-powers` to see what LHM exposes."
        )
    if reading.status in ("dead", "no_reading"):
        raise SensorDeadError(
            f"Sensor '{CPU_PACKAGE}' has no usable value (status={reading.status}). "
            "Is LHM running as administrator with the PawnIO driver loaded?"
        )
    assert reading.watts is not None
    return reading.watts


class LhmClient:
    """Small convenience wrapper holding the URL and timeout."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.url = url
        self.timeout = timeout

    def fetch(self) -> Node:
        return fetch_tree(self.url, self.timeout)

    def read_powers(self) -> dict[str, SensorReading]:
        return read_cpu_powers(self.fetch())

    def package_watts(self) -> float:
        return package_watts_from_tree(self.fetch())
