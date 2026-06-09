"""FastAPI Dashboard endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import load_dashboard


client = TestClient(app)


def test_health_endpoint():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_dashboard_default_renders_latest_month():
    r = client.get("/")
    assert r.status_code == 200
    assert "Dennis FISHER" in r.text
    # June is the latest bundled month — should be selected
    assert "June 2026" in r.text


def test_dashboard_switcher_loads_may_via_ym():
    r = client.get("/?ym=2026-5")
    assert r.status_code == 200
    assert "Dashboard — May 2026" in r.text
    # May line value is 65.29
    assert "65.29" in r.text
    # And total pay 65.29 × $124.59 = $8,134.48
    assert "$8,134.48" in r.text


def test_dashboard_switcher_loads_june():
    r = client.get("/?ym=2026-6")
    assert r.status_code == 200
    assert "Dashboard — June 2026" in r.text
    assert "65.78" in r.text
    assert "$8,195.53" in r.text


def test_dashboard_accepts_explicit_year_month_params():
    r = client.get("/?year=2026&month=5")
    assert r.status_code == 200
    assert "May 2026" in r.text


def test_invalid_ym_returns_400():
    r = client.get("/?ym=garbage")
    assert r.status_code == 400


def test_unknown_month_returns_404():
    r = client.get("/?year=2030&month=1")
    assert r.status_code == 404
    assert "No data bundled" in r.json()["detail"]


def test_dashboard_renders_winning_option_label():
    """The greater-of-three table should highlight the winning row."""
    r = client.get("/?ym=2026-6")
    assert r.status_code == 200
    # June: Floor wins (option1 == option3 == 65.78, first-equal logic).
    assert "winning: Guarantee floor" in r.text


def test_dashboard_status_strip_reports_loaded_sources():
    r = client.get("/?ym=2026-6")
    assert r.status_code == 200
    # June has all three sources bundled
    assert "Final Award loaded" in r.text
    assert "Trip Pairing Packet loaded" in r.text
    assert "iCal feed loaded" in r.text
    # Clean packet → 0 discrepancies
    assert "0 discrepancies" in r.text


def test_services_load_dashboard_caches_repeat_calls():
    """The lru_cache should make a second call cheap (same object identity)."""
    # Clear any previous cache state so the test is hermetic
    load_dashboard.cache_clear()
    first = load_dashboard(2026, 6)
    second = load_dashboard(2026, 6)
    assert first is second
