"""Parser and HTTP client tests (fixture + synthetic variants + mock server)."""

from __future__ import annotations

import socket

import pytest

from greensettle.agent import lhm_client as lhm
from tests.mock_lhm import set_package_watts


def powers_group(tree):
    return lhm.find_powers_group(lhm.find_cpu_node(tree))


def sensor(tree, name):
    return next(s for s in powers_group(tree)["Children"] if s["Text"] == name)


# ---------------------------------------------------------------- fixture
def test_fixture_finds_cpu_and_powers(fixture_tree):
    cpu = lhm.find_cpu_node(fixture_tree)
    assert cpu["HardwareId"] == "/intelcpu/0"
    assert [s["Text"] for s in powers_group(fixture_tree)["Children"]] == list(lhm.CPU_POWER_SENSORS)


def test_fixture_reads_four_sensors_with_status(fixture_tree):
    r = lhm.read_cpu_powers(fixture_tree)
    assert r["CPU Package"].watts == pytest.approx(4.5) and r["CPU Package"].status == "ok"
    assert r["CPU Cores"].status == "ok"
    assert r["CPU Memory"].status == "dead"
    assert r["CPU Platform"].status == "dead"


def test_gpu_powers_group_is_not_mistaken_for_cpu(fixture_tree):
    # The fixture also has a GPU with its own "Powers" group; it must be ignored.
    names = [s["Text"] for s in powers_group(fixture_tree)["Children"]]
    assert "GPU Power" not in names


def test_package_watts_from_tree(fixture_tree):
    assert lhm.package_watts_from_tree(fixture_tree) == pytest.approx(4.5)


# ---------------------------------------------------------------- number parsing
@pytest.mark.parametrize(
    "text, expected",
    [
        ("4.5 W", 4.5),
        ("4,5 W", 4.5),  # German/Indian-English-on-German-locale style decimal comma
        ("39,7W", 39.7),
        ("1.234,5 W", 1234.5),
        ("1,234.5 W", 1234.5),
        ("-3.2 W", -3.2),
        (7, 7.0),
        (4.25, 4.25),
        ("12", 12.0),
        ("-", None),
        ("", None),
        ("  ", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_number(text, expected):
    assert lhm.parse_number(text) == expected


# ---------------------------------------------------------------- synthetic variants
def test_comma_decimal_value_when_raw_is_null(fixture_tree):
    s = sensor(fixture_tree, "CPU Package")
    s["RawValue"] = None
    s["Value"] = "12,3 W"
    assert lhm.package_watts_from_tree(fixture_tree) == pytest.approx(12.3)


def test_raw_value_is_preferred_over_formatted_value(fixture_tree):
    s = sensor(fixture_tree, "CPU Package")
    s["RawValue"] = 9.87
    s["Value"] = "9.9 W"  # rounded for display
    assert lhm.package_watts_from_tree(fixture_tree) == pytest.approx(9.87)


def test_raw_value_as_string_is_accepted(fixture_tree):
    sensor(fixture_tree, "CPU Package")["RawValue"] = "8.5"
    assert lhm.package_watts_from_tree(fixture_tree) == pytest.approx(8.5)


def test_null_raw_and_dash_value_is_no_reading(fixture_tree):
    s = sensor(fixture_tree, "CPU Package")
    s["RawValue"] = None
    s["Value"] = "-"
    assert lhm.read_cpu_powers(fixture_tree)["CPU Package"].status == "no_reading"
    with pytest.raises(lhm.SensorDeadError):
        lhm.package_watts_from_tree(fixture_tree)


def test_dead_package_raises(fixture_tree):
    set_package_watts(fixture_tree, 0.0)
    with pytest.raises(lhm.SensorDeadError):
        lhm.package_watts_from_tree(fixture_tree)


def test_missing_sensor(fixture_tree):
    group = powers_group(fixture_tree)
    group["Children"] = [s for s in group["Children"] if s["Text"] != "CPU Package"]
    readings = lhm.read_cpu_powers(fixture_tree)
    assert readings["CPU Package"].status == "missing"
    assert readings["CPU Package"].watts is None
    assert readings["CPU Cores"].status == "ok"  # others still read
    with pytest.raises(lhm.SensorMissingError):
        lhm.package_watts_from_tree(fixture_tree)


def test_empty_tree_does_not_crash():
    for tree in ({}, {"Text": "Sensor", "Children": []}, {"Children": None}):
        readings = lhm.read_cpu_powers(tree)
        assert all(r.status == "missing" for r in readings.values())
        with pytest.raises(lhm.SensorMissingError):
            lhm.package_watts_from_tree(tree)


def test_cpu_found_by_icon_when_hardware_id_absent(fixture_tree):
    del lhm.find_cpu_node(fixture_tree)["HardwareId"]
    assert lhm.find_cpu_node(fixture_tree)["Text"] == "Intel Core Ultra (synthetic)"


# ---------------------------------------------------------------- HTTP
def test_client_against_mock_server(mock_lhm):
    client = lhm.LhmClient(mock_lhm.url)
    assert client.package_watts() == pytest.approx(4.5)
    mock_lhm.set_package_watts(25.0)
    assert client.package_watts() == pytest.approx(25.0)
    assert client.read_powers()["CPU Memory"].status == "dead"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unreachable_server_has_helpful_message():
    url = f"http://127.0.0.1:{_free_port()}/data.json"  # nothing listens here
    with pytest.raises(lhm.LhmUnreachableError, match="Remote Web Server"):
        lhm.fetch_tree(url, timeout=1.0)


def test_url_resolution_order(monkeypatch):
    monkeypatch.delenv(lhm.URL_ENV_VAR, raising=False)
    assert lhm.resolve_url(None) == lhm.DEFAULT_URL
    monkeypatch.setenv(lhm.URL_ENV_VAR, "http://env:1/data.json")
    assert lhm.resolve_url(None) == "http://env:1/data.json"
    assert lhm.resolve_url("http://flag:2/data.json") == "http://flag:2/data.json"
