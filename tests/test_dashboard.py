"""Phase 4 Part A: dashboard page and its read-only admin endpoints."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from greensettle.server.app import DASHBOARD_FILE
from tests.test_server_api import ADM_HDR, A_HDR, Server

HTML = DASHBOARD_FILE.read_text(encoding="utf-8")


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    with s.client:
        yield s


# ---------------------------------------------------------------- the static page
def test_dashboard_page_is_served_with_a_strict_policy(srv):
    r = srv.client.get("/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.text == HTML


def test_dashboard_has_no_external_resources_and_no_storage():
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", HTML))
    assert urls == {"http://www.w3.org/2000/svg"}  # the SVG namespace name, not a request
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", ".innerHTML", "<link", "@import"):
        assert forbidden not in HTML, forbidden
    assert 'type="password"' in HTML  # the token field is masked


def test_dashboard_labels_simulated_values():
    assert HTML.count("simulated") >= 8
    for panel in ("Clock and grid", "Live power", "Queue", "Settlements", "Users", "Explain a settlement"):
        assert panel in HTML


# ---------------------------------------------------------------- read-only endpoints need the admin token
@pytest.mark.parametrize("path", ["/admin/history", "/admin/intensity_curve", "/admin/state"])
def test_new_endpoints_require_admin(srv, path):
    srv.user("alice")
    for headers in ({}, A_HDR, {"X-Admin-Token": "wrong-token-xxxxxxxx"}):
        assert srv.client.get(path, headers=headers).status_code == 401


def test_intensity_curve_shape_and_overrides(srv):
    srv.client.post("/admin/intensity", json={"state": "dirty"}, headers=ADM_HDR)
    srv.advance_sim(timedelta(hours=1))
    srv.client.post("/admin/intensity", json={"state": "off"}, headers=ADM_HDR)
    c = srv.client.get("/admin/intensity_curve?hours=24", headers=ADM_HDR).json()
    assert c["simulated"] is True and "simulated" in c["note"]
    assert len(c["actual"]) == 24 * 4 + 1  # every 15 sim minutes
    assert all(p["t"] >= c["now"] for p in c["forecast"])  # forecast only for the future
    assert c["overrides"] == [{"from": "2026-11-02T06:00:00+00:00", "to": "2026-11-02T07:00:00+00:00",
                               "state": "dirty"}]
    dirty = [p for p in c["actual"] if p["t"].startswith("2026-11-02T06")]
    assert dirty and all(p["g"] == 700.0 for p in dirty)
    assert srv.client.get("/admin/intensity_curve?hours=0", headers=ADM_HDR).status_code == 422


def test_history_and_state_carry_what_the_panels_need(srv):
    alice = srv.user("alice")
    srv.submit(alice)  # stand-in choose: deferred 1 h, two candidates
    st = srv.client.get("/admin/state", headers=ADM_HDR).json()
    q = st["queue"][0]
    assert q["plan"]["reason"] == "deferred" and len(q["plan"]["candidates"]) == 2
    assert q["max_delay_h"] == 4 and q["est_duration_s"] == 2
    u = st["users"][0]
    assert u["balance_series"][-1][1] == 100.0 and u["decay_total"] == 0.0 and u["last_decay_at"]

    srv.advance_sim(timedelta(hours=1))
    srv.state.tick()
    job = srv.claim().json()["job_id"]
    srv.result(job)
    hist = srv.client.get("/admin/history?n=5", headers=ADM_HDR).json()
    assert len(hist) == 1
    row = hist[0]
    for key in ("energy_kwh", "energy_unc_kwh", "i_submit", "i_submit_unc", "i_run", "i_run_unc",
                "saved_g", "saved_unc_g", "fee", "rebate", "surcharge", "net", "zone", "plan",
                "run_start_sim", "run_end_sim", "submitted_at"):
        assert row[key] is not None, key
    assert row["settlement_params"]["k"] == 1.0 and row["params"] == {"seconds": 2, "workers": 1}
    assert row["net"] == pytest.approx(row["fee"] + row["surcharge"] - row["rebate"])
    after = srv.client.get("/admin/state", headers=ADM_HDR).json()["users"][0]
    assert after["decay_total"] > 0  # one sim hour of decay was applied before booking
