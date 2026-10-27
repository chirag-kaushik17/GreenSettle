"""Real-hardware checks. Skipped unless GREENSETTLE_HARDWARE=1.

Run on the Windows laptop with LibreHardwareMonitor's web server running:

    $env:GREENSETTLE_HARDWARE = "1"; python -m pytest -m hardware -v
"""

from __future__ import annotations

import pytest

from greensettle.agent import lhm_client as lhm
from greensettle.agent import meter

pytestmark = pytest.mark.hardware


@pytest.fixture
def client() -> lhm.LhmClient:
    return lhm.LhmClient(lhm.resolve_url())


def test_real_server_reachable_and_package_live(client):
    readings = client.read_powers()
    assert readings["CPU Package"].status == "ok"
    assert readings["CPU Cores"].status == "ok"


def test_real_package_power_is_plausible(client):
    # Sanity range only: idle was ~4.5 W, all-core peak 39.7 W on this laptop.
    assert 1.0 < client.package_watts() < 80.0


def test_real_sampler_short_run(client):
    import time

    sampler = meter.Sampler(client.package_watts, interval_s=1.0)
    sampler.start()
    time.sleep(5)
    sampler.stop()
    stats = sampler.stats()
    assert stats.samples >= 5
    assert stats.mean_interval_s == pytest.approx(1.0, abs=0.2)
    print(f"\nunchanged fraction on real LHM: {stats.unchanged_fraction:.0%}, jitter {stats.jitter_s * 1000:.1f} ms")
