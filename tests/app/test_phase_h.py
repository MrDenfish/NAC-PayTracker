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
