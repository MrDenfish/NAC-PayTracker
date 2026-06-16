"""Phase I — premium pay visibility tests."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.app.services import (
    _categorize,
    _pipeline,
    invalidate_caches,
    load_calendar,
    load_dashboard,
    load_pay_breakdown,
)
from nac_pay.auth import get_email_sender
from nac_pay.engine import ChunkKind, ChunkResult
from nac_pay.onboarding import mark_completed
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _docs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _bootstrap(monkeypatch, email: str) -> tuple[TestClient, str]:
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("STRIPE_BACKEND", "fake")
    client = TestClient(app)
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password",
              "confirm": "long enough password"},
        follow_redirects=False,
    )
    client.get(f"/verify/{_verify_token(get_email_sender().sent[-1].body)}",
               follow_redirects=False)
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.email == email.lower())
        ).scalar_one()
        row.subscription_status = "ACTIVE"
        uid = row.user_id
    mark_completed(uid)
    client.post(
        "/settings",
        data={"name": "Test", "position": "FO", "hourly_rate": "124.59",
              "pilot_id": "DFI", "sick_bank_days": "0", "pto_bank_days": "0",
              "feed_url": "", "feed_auto_update": ""},
        follow_redirects=False,
    )
    for kind, name, source in [
        ("FINAL_AWARD", "fa.pdf", "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf"),
        ("TRIP_PACKET", "p.pdf", "JUNE 2026 Trip Pairing Packet.pdf"),
        ("ICAL_FEED", "f.ics", "iCal_schedule_feed.ics"),
    ]:
        client.post(
            "/documents/upload",
            data={"year": "2026", "month": "6", "kind": kind},
            files={"upload": (name, (_docs_dir() / source).read_bytes(),
                              "application/octet-stream")},
            follow_redirects=False,
        )
    invalidate_caches()
    return client, uid


def _reassign(client: TestClient, date_iso: str, *, aid: str, pch: str,
              premium: str = "NONE", reason: str = "FLOWN") -> None:
    client.post(
        f"/day/{date_iso}/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": aid, "pch_value": pch,
              "reason_code": reason, "premium_category": premium},
        follow_redirects=False,
    )


# ── I.1 — categorization bug fix ─────────────────────────────────────


def test_categorize_overtime_is_not_regular_pay():
    """Phase I.1: a chunk with multiplier=1.5 and premium_category=OVERTIME
    should categorize as 'Overtime', not 'Regular Pay'."""
    chunk = ChunkResult(
        source_id="x", kind=ChunkKind.TRIP,
        raw_pch=Decimal("5.0"), multiplier=Decimal("1.5"),
        rate=Decimal("186.885"), dollars=Decimal("934.43"),
        premium_category="OVERTIME",
    )
    assert _categorize(chunk) == "Overtime"


def test_categorize_landing_credit():
    chunk = ChunkResult(
        source_id="x", kind=ChunkKind.TRIP,
        raw_pch=Decimal("1.0"), multiplier=Decimal("1.5"),
        rate=Decimal("186.885"), dollars=Decimal("186.89"),
        premium_category="LANDING",
    )
    assert _categorize(chunk) == "Landing Credit"


def test_categorize_junior_assignment_1st():
    chunk = ChunkResult(
        source_id="x", kind=ChunkKind.TRIP,
        raw_pch=Decimal("5.0"), multiplier=Decimal("2.0"),
        rate=Decimal("249.18"), dollars=Decimal("1245.90"),
        premium_category="JUNIOR_ASSIGNMENT_1ST",
    )
    assert _categorize(chunk) == "Junior Assignment"


def test_open_time_via_off_day_reassignment(monkeypatch):
    """End-to-end: reassign two OFF days to 1021 RES with Open Time
    premium. Pay Breakdown shows 'Open Time' row totaling 7.64 PCH,
    NOT 'Regular Pay 1.5x'."""
    client, uid = _bootstrap(monkeypatch, "open-time-e2e@x.test")
    _reassign(client, "2026-06-07", aid="1021 RES", pch="3.82",
              premium="OPEN_TIME_MID_MONTH")
    _reassign(client, "2026-06-14", aid="1021 RES", pch="3.82",
              premium="OPEN_TIME_MID_MONTH")

    pb = load_pay_breakdown(2026, 6, uid)
    open_time_rows = [r for r in pb.earning_rows if r.pay_type == "Open Time"]
    assert len(open_time_rows) == 1
    assert open_time_rows[0].pch == Decimal("7.64")
    assert open_time_rows[0].multiplier == Decimal("1.5")

    # NO "Regular Pay" row at 1.5× should exist.
    bad = [r for r in pb.earning_rows
           if r.pay_type == "Regular Pay" and r.multiplier > Decimal("1.0")]
    assert bad == []


# ── I.2 — dashboard split ────────────────────────────────────────────


def test_dashboard_split_regular_vs_premium(monkeypatch):
    client, uid = _bootstrap(monkeypatch, "dash-split@x.test")
    _reassign(client, "2026-06-07", aid="X", pch="4.00",
              premium="OPEN_TIME_MID_MONTH")
    dash = load_dashboard(2026, 6, uid)
    assert dash.premium_pch == Decimal("4.00")
    assert dash.regular_pch > Decimal("0")
    # Total PCH = regular + premium (within the engine's rounding tolerance).
    assert (dash.regular_pch + dash.premium_pch) == dash.option3_earned
    # Premium dollars matches the multiplier path.
    assert dash.premium_dollars == (
        Decimal("4.00") * Decimal("124.59") * Decimal("1.5")
    ).quantize(Decimal("0.01"))


def test_dashboard_no_premium_default():
    """Default user has no reassignments → premium_pch is 0."""
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "regular" in body.lower()


# ── I.3 — Pay Breakdown comprehensive ────────────────────────────────


def test_categorize_classroom_train_split():
    chunk = ChunkResult(
        source_id="x", kind=ChunkKind.TRAINING,
        raw_pch=Decimal("12.0"), multiplier=Decimal("1.0"),
        rate=Decimal("124.59"), dollars=Decimal("1495.08"),
        label="CLASS",
    )
    assert _categorize(chunk) == "Classroom Train"


def test_categorize_simulator_train_split():
    chunk = ChunkResult(
        source_id="x", kind=ChunkKind.TRAINING,
        raw_pch=Decimal("21.0"), multiplier=Decimal("1.0"),
        rate=Decimal("124.59"), dollars=Decimal("2616.39"),
        label="SIM",
    )
    assert _categorize(chunk) == "Simulator Train"


# ── I.4 / I.5 — calendar cell premium label + $ ─────────────────────


def test_calendar_cell_renders_premium_label(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "cal-premium@x.test")
    _reassign(client, "2026-06-07", aid="1021 RES", pch="3.82",
              premium="OPEN_TIME_MID_MONTH")
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    assert "premium-label" in body
    assert "Open Time" in body


def test_calendar_cell_renders_per_day_dollars(monkeypatch):
    """Phase I.5: each cell shows a whole-dollar pay value."""
    client, _ = _bootstrap(monkeypatch, "cal-dollar@x.test")
    _reassign(client, "2026-06-07", aid="1021 RES", pch="3.82",
              premium="OPEN_TIME_MID_MONTH")
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    # 3.82 × $124.59 × 1.5 ≈ $713.90 → rounded to $714
    assert "pay-dollars" in body
    assert "$714" in body


# ── I.6 — calendar footer ────────────────────────────────────────────


def test_calendar_footer_shows_total_pay(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "cal-footer@x.test")
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    assert "Total Pay" in body
    # The MPG-65 label should be GONE.
    assert "Vs MPG 65" not in body


def test_calendar_data_includes_total_pay(monkeypatch):
    client, uid = _bootstrap(monkeypatch, "cal-data@x.test")
    cd = load_calendar(2026, 6, uid)
    # Bundled June total is $8,195.53 (from prior tests).
    assert cd.total_pay == Decimal("8195.53")


# ── I.7 — day-detail pay breakdown card ─────────────────────────────


def test_day_pay_card_for_reassigned_off_day(monkeypatch):
    """Phase I.7 — a reassigned OFF day shows its own pay row."""
    client, _ = _bootstrap(monkeypatch, "day-pay@x.test")
    _reassign(client, "2026-06-07", aid="1021 RES", pch="3.82",
              premium="OPEN_TIME_MID_MONTH")
    r = client.get("/day/2026-06-07")
    body = r.text
    assert "Day pay" in body
    # Row contents: category + PCH + amount
    assert "Open Time" in body
    assert "3.82" in body
    # 3.82 × 124.59 × 1.5 = $713.90
    assert "$713.90" in body


def test_day_pay_card_for_regular_trip_day(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "day-pay-trip@x.test")
    r = client.get("/day/2026-06-02")
    body = r.text
    assert "Day pay" in body
    assert "Regular Pay" in body


def test_day_pay_scoped_to_single_trip_occurrence(monkeypatch):
    """Regression: trip_id collisions across dates must not pool chunks.

    The bundled June FA has trip_id '722/750' on BOTH June 2 and June 5
    as separate Trip objects. Before the source_id fix, the Day Pay
    card summed chunks from BOTH dates on each day (showing 11.0 PCH
    instead of the per-day 4.92 / 6.08).
    """
    from nac_pay.app.services import load_day, invalidate_caches
    client, uid = _bootstrap(monkeypatch, "trip-id-collision@x.test")

    # Reassign June 2 to a higher PCH; June 5 is left alone.
    _reassign(client, "2026-06-02", aid="722/754", pch="6.08",
              premium="NONE", reason="REASSIGNMENT")

    invalidate_caches()
    # June 2 — should show ONLY the reassigned trip's PCH (6.08).
    dd2 = load_day(2026, 6, 2, user_id=uid)
    assert dd2.day_pay_total is not None
    # The reassignment wins (6.08 > 4.92), so the day shows ~6.08 × $124.59.
    expected_2 = Decimal("6.08") * Decimal("124.59")
    assert abs(dd2.day_pay_total - expected_2) < Decimal("0.10"), \
        f"June 2 should pay ~${expected_2}, got ${dd2.day_pay_total}"

    # June 5 — unmodified, should show the original 4.92 PCH × rate.
    dd5 = load_day(2026, 6, 5, user_id=uid)
    assert dd5.day_pay_total is not None
    expected_5 = Decimal("4.92") * Decimal("124.59")
    assert abs(dd5.day_pay_total - expected_5) < Decimal("0.10"), \
        f"June 5 should pay ~${expected_5}, got ${dd5.day_pay_total}"

    # And critically: the two days should NOT sum to the same total.
    assert dd2.day_pay_total != dd5.day_pay_total
