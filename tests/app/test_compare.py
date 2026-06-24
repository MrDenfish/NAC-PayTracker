"""Compare-to-pay-stub view tests."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import (
    CompareVerdict,
    combine_stubs,
    load_compare,
)
from nac_pay.parsers import parse_pay_stub

D = Decimal
client = TestClient(app)


# ── combine_stubs ──────────────────────────────────────────────────────


def test_combine_stubs_nets_mpg_advance_to_actual_regular_pay():
    """May stubs together: +32.50 (advance) + 80.29 (actual) + -32.50 (reversal)
    = 80.29 hrs Regular Pay = $10,003.33."""
    from pathlib import Path
    DOCS = Path(__file__).resolve().parents[2] / "docs" / "pay Stubs"
    stubs = (
        parse_pay_stub(DOCS / "May_ Base_payStub.pdf"),
        parse_pay_stub(DOCS / "May_payStub.pdf"),
    )
    summary = combine_stubs(stubs)
    reg_hours, reg_amount = summary.by_category["Regular Pay"]
    assert reg_hours == D("80.290000")
    assert reg_amount == D("10003.33")
    ot_hours, ot_amount = summary.by_category["Open Time"]
    assert ot_hours == D("3.820000")
    assert ot_amount == D("713.90")
    assert summary.net_pay_sum == D("2061.44") + D("3355.02")


# ── load_compare ──────────────────────────────────────────────────────


def test_load_compare_may_surfaces_under_by_2582_75():
    """May tracker ($8,134.48 baseline) vs stubs ($10,717.23 actuals incl.
    mid-month events not in our iCal sample): tracker should be flagged
    under by exactly $2,582.75. Regular Pay row Δ = -$1,868.85, Open Time
    Δ = -$713.90."""
    d = load_compare(2026, 5)
    assert d.verdict is CompareVerdict.TRACKER_UNDER
    assert d.total_tracker == D("8134.48")
    assert d.total_stub == D("10717.23")
    assert d.total_delta == D("-2582.75")
    assert d.mpg_advance_netted is True

    reg = next(r for r in d.rows if r.pay_type == "Regular Pay")
    assert reg.tracker_amount == D("8134.48")
    assert reg.stub_amount == D("10003.33")
    assert reg.delta_amount == D("-1868.85")
    assert reg.matches is False

    ot = next(r for r in d.rows if r.pay_type == "Open Time")
    assert ot.tracker_amount == D("0.00") or ot.tracker_amount == D("0")
    assert ot.stub_amount == D("713.90")
    assert ot.delta_amount == D("-713.90")


def test_load_compare_excludes_benefit_only_rows():
    """Group Term Life is a benefit, not PCH — must not appear in the
    comparison rows (the spec note explicitly excludes it)."""
    d = load_compare(2026, 5)
    pay_types = {r.pay_type for r in d.rows}
    assert "Group Term Life" not in pay_types


def test_load_compare_june_returns_no_stubs_verdict():
    d = load_compare(2026, 6)
    assert d.verdict is CompareVerdict.NO_STUBS
    assert d.rows == ()
    assert d.stub_chips == ()


def test_load_compare_stub_chips_carry_period_pay_date_net():
    d = load_compare(2026, 5)
    assert len(d.stub_chips) == 2
    assert d.stub_chips[0].label == "2026-05-01 → 2026-05-15"
    assert d.stub_chips[0].pay_date_iso == "2026-05-22"
    assert d.stub_chips[0].net_pay == D("2061.44")
    assert d.stub_chips[1].label == "2026-05-16 → 2026-05-31"


# ── Route ──────────────────────────────────────────────────────────────


def test_compare_route_renders_may_verdict():
    r = client.get("/compare?ym=2026-5")
    assert r.status_code == 200
    assert "Compare to pay stub" in r.text
    assert "$2,582.75 less" in r.text
    assert "$10,003.33" in r.text
    assert "$713.90" in r.text


def test_compare_route_renders_no_stubs_banner_for_june():
    r = client.get("/compare?ym=2026-6")
    assert r.status_code == 200
    assert "No pay stubs bundled" in r.text


def test_compare_route_active_nav():
    r = client.get("/compare?ym=2026-5")
    assert 'href="/compare?ym=2026-5" class="nav-link nav-link--active"' in r.text


def test_compare_route_invalid_ym_400():
    assert client.get("/compare?ym=oops").status_code == 400


def test_compare_route_unknown_month_404():
    assert client.get("/compare?year=2030&month=1").status_code == 404
