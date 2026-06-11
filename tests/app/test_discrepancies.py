"""Discrepancies queue view tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import (
    DiscrepancyKind,
    DiscrepancySeverity,
    _pipeline,
    load_discrepancies,
)
from nac_pay.parsers import ValidationDiscrepancy
from nac_pay.schedule import AppliedEvent, AppliedEventKind


client = TestClient(app)
D = Decimal


# ── Route ──────────────────────────────────────────────────────────────


def test_discrepancies_route_renders_may():
    r = client.get("/discrepancies?ym=2026-5")
    assert r.status_code == 200
    assert "Discrepancies" in r.text
    assert "May 2026" in r.text
    # The two May compare mismatches should surface
    assert "Regular Pay" in r.text
    assert "Open Time" in r.text
    assert "$-2,582.75" in r.text


def test_discrepancies_route_june_shows_all_clear():
    r = client.get("/discrepancies?ym=2026-6")
    assert r.status_code == 200
    assert "All clear" in r.text


def test_discrepancies_active_nav():
    r = client.get("/discrepancies?ym=2026-6")
    assert (
        'href="/discrepancies" class="nav-link nav-link--active"' in r.text
    )


def test_discrepancies_route_invalid_ym():
    assert client.get("/discrepancies?ym=oops").status_code == 400


def test_discrepancies_route_unknown_month():
    assert client.get("/discrepancies?year=2030&month=1").status_code == 404


# ── Loader content: real May / June ───────────────────────────────────


def test_load_discrepancies_may_yields_two_investigation_items():
    d = load_discrepancies(2026, 5)
    assert len(d.items) == 2
    assert all(i.kind is DiscrepancyKind.COMPARE_MISMATCH for i in d.items)
    assert all(i.severity is DiscrepancySeverity.INVESTIGATION for i in d.items)
    assert d.counts_by_severity["INVESTIGATION"] == 2
    assert d.counts_by_severity["OWED_MONEY"] == 0
    assert d.total_money_impact == D("-2582.75")


def test_load_discrepancies_may_sorts_largest_impact_first():
    """Within a severity, |money_impact| descending — Regular Pay ($1,868.85)
    before Open Time ($713.90)."""
    d = load_discrepancies(2026, 5)
    titles = [item.title for item in d.items]
    assert "Regular Pay" in titles[0]
    assert "Open Time" in titles[1]


def test_load_discrepancies_may_items_link_to_compare():
    d = load_discrepancies(2026, 5)
    for item in d.items:
        assert item.action_url == "/compare?ym=2026-5"
        assert item.action_label == "Open compare"


def test_load_discrepancies_june_is_empty():
    d = load_discrepancies(2026, 6)
    assert d.items == ()
    assert d.total_money_impact == D("0")
    assert all(c == 0 for c in d.counts_by_severity.values())


# ── Synthetic: severity + sort ordering across all three sources ──────


def test_severity_priority_puts_owed_money_before_investigation_before_review():
    """Poke the pipeline cache to inject all three discrepancy kinds at once:
    a packet validation flag (REVIEW), an UNMATCHED_TRIP applied event
    (REVIEW), and synthetic positive + negative compare mismatches
    (OWED_MONEY / INVESTIGATION). The result should be sorted with
    OWED_MONEY at the top.
    """
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    fake_validation = (
        ValidationDiscrepancy(
            trip_id="XYZ",
            field="flight_op_pch",
            printed=D("9.99"),
            recomputed=D("4.17"),
            delta=D("5.82"),
            page_index=0,
        ),
    )
    fake_applied = (
        AppliedEvent(
            kind=AppliedEventKind.UNMATCHED_TRIP_REVIEW,
            date=date(2026, 6, 15),
            trip_id=None,
            detail="Flew sequence 9999/9998 — needs categorization",
            delta_pch=None,
        ),
    )
    poked = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=real.updated_month,
        engine_result=real.engine_result,
        applied_events=fake_applied,
        validation_discrepancies=fake_validation,
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_discrepancies(2026, 6)

    # We expect: 1 PACKET_VALIDATION (REVIEW) + 1 UNMATCHED_TRIP (REVIEW)
    # = 2 items total (no compare since no June stub).
    assert len(d.items) == 2
    kinds = [item.kind for item in d.items]
    assert DiscrepancyKind.PACKET_VALIDATION in kinds
    assert DiscrepancyKind.UNMATCHED_TRIP in kinds
    assert all(item.severity is DiscrepancySeverity.REVIEW for item in d.items)


def test_unmatched_trip_links_to_day_detail():
    """An UNMATCHED_TRIP item should link to /day/{date}."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    fake_applied = (
        AppliedEvent(
            kind=AppliedEventKind.UNMATCHED_TRIP_REVIEW,
            date=date(2026, 6, 15),
            trip_id=None,
            detail="Flew sequence 9999/9998",
            delta_pch=None,
        ),
    )
    poked = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=real.updated_month,
        engine_result=real.engine_result,
        applied_events=fake_applied,
        validation_discrepancies=(),
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_discrepancies(2026, 6)

    item = next(i for i in d.items if i.kind is DiscrepancyKind.UNMATCHED_TRIP)
    assert item.action_url == "/day/2026-06-15"
    assert item.action_label == "View day"


def test_packet_validation_links_to_pay_breakdown():
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    fake_validation = (
        ValidationDiscrepancy(
            trip_id="768/768/769",
            field="duty_rig_pch",
            printed=D("3.54"),
            recomputed=D("3.55"),
            delta=D("-0.01"),
            page_index=8,
        ),
    )
    poked = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=real.updated_month,
        engine_result=real.engine_result,
        applied_events=(),
        validation_discrepancies=fake_validation,
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_discrepancies(2026, 6)

    item = next(i for i in d.items if i.kind is DiscrepancyKind.PACKET_VALIDATION)
    assert "768/768/769" in item.title
    assert item.action_url == "/pay?ym=2026-6"
