"""Phase H — OFF-day reassignment, calendar badge, packet datalist,
per-version trip structure on day-detail."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.app.services import _pipeline, invalidate_caches
from nac_pay.auth import get_email_sender
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
    """Sign up, verify, set ACTIVE + onboarded, pilot_id=DFI, upload
    bundled June 2026 docs."""
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


def _june_total_pch(uid: str) -> Decimal:
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    return pr.engine_result.option3_earned


# ── OFF-day reassignment ─────────────────────────────────────────────


def test_form_is_available_on_off_days(monkeypatch):
    """Phase G gated the form to trip days. Phase H opens it for OFF
    days too — June 7 in our bundled data is OFF."""
    client, _ = _bootstrap(monkeypatch, "off-form@x.test")
    r = client.get("/day/2026-06-07")
    body = r.text
    assert "Reassign / record a new version" in body or "Correcting" in body
    assert 'action="/day/2026-06-07/reassign"' in body


def test_off_day_reassignment_lifts_day_pch(monkeypatch):
    """Reassign an OFF day to a flight worth 5.00 PCH. The day's
    pch_value goes from 0 → 5.00 via the engine integration; the month
    total goes up by 5.00."""
    client, uid = _bootstrap(monkeypatch, "off-lift@x.test")
    before = _june_total_pch(uid)

    r = client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "OPEN", "pch_value": "5.00",
              "reason_code": "FLOWN", "premium_category": "NONE"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    after = _june_total_pch(uid)
    assert after - before == Decimal("5.00")


def test_off_day_reassignment_preserves_duty_type(monkeypatch):
    """Engine integration lifts Day.pch_value but does NOT change
    duty_type — the calendar continues to show the FA-original
    assignment (per the user's audit requirement)."""
    client, uid = _bootstrap(monkeypatch, "off-preserve@x.test")
    client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "4.00",
              "reason_code": "FLOWN", "premium_category": "NONE"},
        follow_redirects=False,
    )
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    from datetime import date
    day = next(d for d in pr.updated_month.days if d.date == date(2026, 6, 7))
    from nac_pay.schedule.labels import DutyType
    assert day.duty_type is DutyType.OFF
    assert day.pch_value == Decimal("4.00")


# ── Calendar badge ──────────────────────────────────────────────────


def test_calendar_shows_reassignment_badge(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "cal-badge@x.test")
    # Add a reassignment on June 2 (a trip day).
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "6.08",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    assert "day-cell--user-reassigned" in body
    assert "↻1" in body


def test_calendar_badge_counts_active_versions_only(monkeypatch):
    """Correction adds a row but doesn't add a count — the corrected
    version is superseded, leaving N active versions = total - corrections."""
    client, _ = _bootstrap(monkeypatch, "cal-count@x.test")
    # v1 reassign
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "A", "pch_value": "5.00",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    # v2 reassign (typo)
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "B", "pch_value": "5.30",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    # v3 correction of v2
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "2",
              "entry_mode": "SIMPLE", "assignment_id": "B",
              "pch_value": "5.20",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    # 3 versions total, 1 superseded → 2 active.
    assert "↻2" in body
    assert "↻3" not in body


def test_calendar_no_badge_when_no_reassignments(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "cal-nobadge@x.test")
    r = client.get("/calendar?ym=2026-6")
    assert "day-cell--user-reassigned" not in r.text
    # The ↻ character may appear in other UI; specifically the count
    # pattern shouldn't.
    assert "↻1" not in r.text


# ── Datalist for packet trips ───────────────────────────────────────


def test_day_detail_renders_packet_datalist(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "datalist@x.test")
    r = client.get("/day/2026-06-02")
    body = r.text
    # The datalist exists with packet trip options. Bundled June packet
    # has ≥10 trips, so we should see several option elements.
    assert 'id="packet-trip-options"' in body
    assert body.count("data-pch=") >= 5
    # The assignment_id input is wired to the datalist.
    assert 'list="packet-trip-options"' in body


def test_packet_options_include_pch_data_attrs(monkeypatch):
    """The data-pch attribute carries the trip's PCH for JS auto-fill."""
    client, _ = _bootstrap(monkeypatch, "datalist-data@x.test")
    r = client.get("/day/2026-06-02")
    body = r.text
    # Match: <option value="...something..." data-pch="N.NN" ...>
    matches = re.findall(r'<option value="([^"]+)"\s+data-pch="([\d.]+)"', body)
    assert len(matches) >= 5
    # PCH values look reasonable (positive numbers).
    for trip_id, pch in matches[:5]:
        assert Decimal(pch) > 0


# ── Per-version trip structure ──────────────────────────────────────


def test_history_row_shows_packet_match_section(monkeypatch):
    """If a pilot reassignment's assignment_id matches a packet trip,
    the history row's expander shows the packet's structural data."""
    client, _ = _bootstrap(monkeypatch, "vd-packet@x.test")

    # Discover an actual packet trip_id by inspecting the page.
    r = client.get("/day/2026-06-02")
    matches = re.findall(r'<option value="([^"]+)"\s+data-pch="([\d.]+)"', r.text)
    assert matches, "expected packet options to be exposed"
    trip_id, packet_pch = matches[0]

    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": trip_id, "pch_value": packet_pch,
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02")
    body = r.text
    assert "version-detail" in body
    assert "Packet match" in body
    assert trip_id in body


def test_history_row_shows_detailed_inputs_for_detailed_mode(monkeypatch):
    client, _ = _bootstrap(monkeypatch, "vd-detailed@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "DETAILED",
              "assignment_id": "X", "block_hours": "4.00",
              "duty_hours": "14.00", "tafb_hours": "15.00",
              "deadhead_pch": "0", "workdays": "1",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02")
    body = r.text
    assert "Pilot entry — Detailed" in body
    # The detailed inputs appear in the expander.
    assert "4.00 h" in body  # block
    assert "14.00 h" in body  # duty


def test_history_row_shows_offpacket_placeholder_for_simple(monkeypatch):
    """SIMPLE entry with an off-packet ID → no leg/component data
    available, placeholder copy is shown."""
    client, _ = _bootstrap(monkeypatch, "vd-offpacket@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "NOT_IN_PACKET", "pch_value": "5.00",
              "reason_code": "FLOWN", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02")
    body = r.text
    assert "off-packet" in body.lower() or "Off-Packet" in body


def test_calendar_cell_shows_new_assignment_in_bold(monkeypatch):
    """Phase H.1: when a day has a winning pilot reassignment, the
    calendar cell shows the new assignment_id in bold above the
    FA-original label."""
    client, _ = _bootstrap(monkeypatch, "cell-bold@x.test")
    client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "1021 RES", "pch_value": "3.82",
              "reason_code": "FLOWN", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/calendar?ym=2026-6")
    body = r.text
    # The new assignment appears with the bold class + value.
    assert "aid-new" in body
    assert "1021 RES" in body
    # The original duty label (OFF) is still shown.
    assert "aid--original" in body or "duty-label--original" in body


def test_premium_category_propagates_to_engine(monkeypatch):
    """Phase H.2: a pilot reassignment with premium_category=OPEN_TIME_MID_MONTH
    should pay at 1.5×. Currently the engine path drops the user's
    premium category; this test guards the fix."""
    client, uid = _bootstrap(monkeypatch, "premium-prop@x.test")
    # Pick an OFF day (June 7) with a premium reassignment.
    client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "OPEN", "pch_value": "4.00",
              "reason_code": "FLOWN",
              "premium_category": "OPEN_TIME_MID_MONTH"},
        follow_redirects=False,
    )
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    # Verify the synthesized Day carries the OPEN_TIME premium.
    from datetime import date
    day = next(d for d in pr.updated_month.days if d.date == date(2026, 6, 7))
    from nac_pay.schedule.labels import PremiumCategory
    assert day.premium_category is PremiumCategory.OPEN_TIME_MID_MONTH

    # And the pay breakdown surfaces an "Open Time" row at 1.5×.
    from nac_pay.app.services import load_pay_breakdown
    pb = load_pay_breakdown(2026, 6, uid)
    open_time = [r for r in pb.earning_rows if r.pay_type == "Open Time"]
    assert len(open_time) == 1
    assert open_time[0].multiplier == Decimal("1.5")
    assert open_time[0].pch == Decimal("4.00")


def test_override_relabels_reassigned_day_premium(monkeypatch):
    """A pilot override is the final word (§7): on a reassigned day whose
    version adopted premium=OVERTIME, saving a DayOverride premium=Open Time
    must win — the override applies AFTER the version folds, so the version
    can't re-stamp its own premium. Guards the pipeline-ordering fix."""
    client, uid = _bootstrap(monkeypatch, "override-relabel@x.test")

    from datetime import date
    from nac_pay.schedule.labels import PremiumCategory
    from nac_pay.app.services import load_pay_breakdown

    # Pick up an OFF day (June 7) and label it Overtime via the version.
    client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "OPEN", "pch_value": "4.00",
              "reason_code": "FLOWN", "premium_category": "OVERTIME"},
        follow_redirects=False,
    )
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    day = next(d for d in pr.updated_month.days if d.date == date(2026, 6, 7))
    # Baseline: the version drives the day's premium.
    assert day.premium_category is PremiumCategory.OVERTIME

    # Now relabel via the day-detail "Reason & premium" card (DayOverride).
    r = client.post(
        "/day/2026-06-07",
        data={"reason_code": "FLOWN",
              "premium_category": "OPEN_TIME_MID_MONTH",
              "entry_mode": "", "custom_multiplier": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    day = next(d for d in pr.updated_month.days if d.date == date(2026, 6, 7))
    # Override wins — premium is now Open Time, not Overtime.
    assert day.premium_category is PremiumCategory.OPEN_TIME_MID_MONTH

    # And the pay breakdown buckets the chunk under "Open Time", not "Overtime".
    pb = load_pay_breakdown(2026, 6, uid)
    assert any(row.pay_type == "Open Time" for row in pb.earning_rows)
    assert not any(row.pay_type == "Overtime" for row in pb.earning_rows)


def test_day_pay_card_renders_inline_pay_type_editor(monkeypatch):
    """The Day pay card carries an inline pay-type quick-edit form so the
    pilot can relabel premium right where the pay is shown. It posts the
    same DayOverride route and carries reason/entry-mode as hidden fields."""
    client, _ = _bootstrap(monkeypatch, "inline-editor@x.test")
    client.post(
        "/day/2026-06-07/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "OPEN", "pch_value": "4.00",
              "reason_code": "FLOWN", "premium_category": "OVERTIME"},
        follow_redirects=False,
    )
    body = client.get("/day/2026-06-07").text
    # Isolate the Day pay card and assert the inline editor lives inside it.
    card = re.search(r'<h2 class="card-title">Day pay</h2>.*?</div>', body, re.S)
    assert card, "Day pay card should render for a day with pay"
    block = card.group(0)
    assert 'class="day-pay-edit"' in block
    assert 'action="/day/2026-06-07"' in block
    assert 'name="premium_category"' in block
    # Hidden fields preserve the other override dimensions on a quick edit.
    assert 'name="reason_code"' in block
    assert 'name="entry_mode"' in block


def test_premium_not_applied_when_original_wins(monkeypatch):
    """If the user reassigns at a LOWER PCH than the original, the
    original wins via §3.E.1.b protection and the user's premium
    category should NOT be adopted."""
    client, uid = _bootstrap(monkeypatch, "premium-original-wins@x.test")
    # June 2 has a trip with published PCH ~4.92. Reassign at 3.00
    # with a premium category — original should still win and the
    # trip stays at NONE.
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "3.00",
              "reason_code": "REASSIGNMENT",
              "premium_category": "OPEN_TIME_MID_MONTH"},
        follow_redirects=False,
    )
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    from datetime import date
    trip = next(t for t in pr.updated_month.trips
                if date(2026, 6, 2) in t.dates)
    from nac_pay.schedule.labels import PremiumCategory
    # Original still wins → premium_category stays at NONE.
    assert trip.premium_category is PremiumCategory.NONE


def test_history_row_shown_for_superseded_versions(monkeypatch):
    """Audit requirement: a corrected (superseded) version still shows
    its own structure in the history — the user can inspect what was
    originally entered even after the correction."""
    client, _ = _bootstrap(monkeypatch, "vd-superseded@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "DETAILED",
              "assignment_id": "X", "block_hours": "5.00",
              "duty_hours": "10.00", "tafb_hours": "12.00",
              "deadhead_pch": "0", "workdays": "1",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "1",
              "entry_mode": "DETAILED", "assignment_id": "X",
              "block_hours": "4.00", "duty_hours": "10.00",
              "tafb_hours": "12.00", "deadhead_pch": "0",
              "workdays": "1",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02")
    body = r.text
    # Both versions should be present with their expanders.
    assert body.count("version-detail") >= 2
    assert "version--superseded" in body
    # Original (5.00 h block) AND corrected (4.00 h) both visible.
    assert "5.00 h" in body and "4.00 h" in body
