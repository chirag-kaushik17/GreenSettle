"""Shared pytest fixtures.

Hardware tests (marked @pytest.mark.hardware) talk to the REAL
LibreHardwareMonitor server and are skipped unless GREENSETTLE_HARDWARE=1.
Everything else runs against MockLhmServer and passes on any OS.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Iterator

import pytest

from tests.mock_lhm import MockLhmServer, load_fixture

HARDWARE_ENV_VAR = "GREENSETTLE_HARDWARE"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(HARDWARE_ENV_VAR) == "1":
        return
    skip = pytest.mark.skip(reason=f"real-hardware test: set {HARDWARE_ENV_VAR}=1 to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fixture_tree() -> dict[str, Any]:
    """A fresh deep copy of fixtures/lhm_data_sample.json (safe to modify)."""
    return copy.deepcopy(load_fixture())


@pytest.fixture
def mock_lhm() -> Iterator[MockLhmServer]:
    server = MockLhmServer().start()
    yield server
    server.stop()
