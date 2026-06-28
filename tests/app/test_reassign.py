"""Phase G end-to-end: /day/<date>/reassign route + engine integration."""

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
from nac_pay.storage import (
    UserAssignmentVersionStore,
    VersionEntryMode,
    VersionType,
    active_versions,
)
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _docs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _bootstrap_user_with_june(monkeypatch, email: str) -> tuple[TestClient, str]:
    """Sign up, verify, mark ACTIVE + onboarded, set pilot_id=DFI, upload
    bundled June 2026 docs. After this fixture the test account renders
    real pay for June against its own uploaded copies."""
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
        data={
            "name": "Test", "position": "FO", "hourly_rate": "124.59",
            "pilot_id": "DFI", "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
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


# ── Route validation ─────────────────────────────────────────────────


def test_reassign_route_simple_success(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "a@x.test")
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "6.08",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/day/2026-06-02?saved=reassign"

    store = UserAssignmentVersionStore(user_id=uid)
    versions = store.list_for_date("2026-06-02")
    assert len(versions) == 1
    assert versions[0].pch_value == Decimal("6.08")
    assert versions[0].assignment_id == "722/754"


def test_reassign_route_detailed_recomputes_pch(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "b@x.test")
    # duty=14 → duty/2=7 (winner), block=4, tafb=15 → 3.06, dpg=3.82
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "DETAILED",
              "assignment_id": "X", "block_hours": "4.00",
              "duty_hours": "14.00", "tafb_hours": "15.00",
              "deadhead_pch": "0", "workdays": "1",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    versions = UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")
    assert versions[0].pch_value == Decimal("7.00")
    assert versions[0].entry_mode is VersionEntryMode.DETAILED


def test_reassign_blocks_default_user():
    client = TestClient(app)  # AUTH_REQUIRED unset → default user
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "5.0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Default%20user" in r.headers["location"]


def test_reassign_rejects_negative_pch(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "c@x.test")
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "-1.0"},
        follow_redirects=False,
    )
    assert "PCH%20must%20be%20positive" in r.headers["location"]


def test_correction_must_reference_existing_seq(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "d@x.test")
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "999",
              "entry_mode": "SIMPLE", "assignment_id": "X",
              "pch_value": "5.0"},
        follow_redirects=False,
    )
    assert "No%20version%20seq" in r.headers["location"]


def test_cannot_correct_a_correction(monkeypatch):
    client, uid = _bootstrap_user_with_june(monkeypatch, "e@x.test")
    store = UserAssignmentVersionStore(user_id=uid)
    v1 = store.save(date_iso="2026-06-02", version_type=VersionType.REASSIGNMENT,
                    assignment_id="X", entry_mode=VersionEntryMode.SIMPLE,
                    pch_value=Decimal("5.0"))
    v2 = store.save(date_iso="2026-06-02", version_type=VersionType.CORRECTION,
                    correction_of=v1.seq, assignment_id="X",
                    entry_mode=VersionEntryMode.SIMPLE,
                    pch_value=Decimal("5.2"))
    r = client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": str(v2.seq),
              "entry_mode": "SIMPLE", "assignment_id": "X",
              "pch_value": "5.1"},
        follow_redirects=False,
    )
    assert "correct%20a%20correction" in r.headers["location"]


# ── Engine integration ──────────────────────────────────────────────


def _june_total_pch(uid: str) -> Decimal:
    """Force-fresh pipeline and return June's monthly PCH (Option 3 earned)."""
    invalidate_caches()
    pr = _pipeline(2026, 6, uid)
    return pr.engine_result.option3_earned


def test_reassignment_increases_effective_pch_via_engine(monkeypatch):
    """Original June 2 trip 722/750 has published PCH ~4.92. A reassignment
    to 6.08 should bump effective PCH to 6.08 via §3.E.1.b max."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "f@x.test")
    before = _june_total_pch(uid)

    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "8.00",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    after = _june_total_pch(uid)
    # June 2 originally ~4.92 PCH; reassignment of 8.00 → +3.08 PCH
    diff = after - before
    assert diff > Decimal("3.0"), f"expected ≈+3.08 PCH, got {diff}"


def test_typo_correction_does_not_inflate_pay(monkeypatch):
    """The Phase G design scenario, end-to-end.
    v1=5.0, v2=5.3 (typo), v3=5.2 (correction). Expected effective=5.2."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "g@x.test")
    base = _june_total_pch(uid)

    # v1
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "5.00",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    # v2 (typo 5.3)
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "X", "pch_value": "5.30",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    after_typo = _june_total_pch(uid)
    # v3 — correction of v2 → 5.2
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "2",
              "entry_mode": "SIMPLE", "assignment_id": "X",
              "pch_value": "5.20",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    after_corr = _june_total_pch(uid)

    # The June 2 trip's effective PCH should now be max(published~4.92, v1=5.0, v3=5.2) = 5.2.
    # NOT 5.3 — that's the typo we corrected out.
    # The delta between base (4.92) and after_corr should be approximately 5.2 - 4.92 = 0.28.
    diff = after_corr - base
    assert Decimal("0.20") <= diff <= Decimal("0.40"), \
        f"effective should reflect 5.20 (corrected), not 5.30 (typo). diff={diff}"

    # And the corrected total must be LESS than the un-corrected typo total.
    assert after_corr < after_typo


def test_audit_trail_preserves_all_versions(monkeypatch):
    """Even after correction, every version is still in the store."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "h@x.test")
    for pch in ("5.0", "5.3"):
        client.post(
            "/day/2026-06-02/reassign",
            data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
                  "assignment_id": "X", "pch_value": pch,
                  "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
            follow_redirects=False,
        )
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "2",
              "entry_mode": "SIMPLE", "assignment_id": "X",
              "pch_value": "5.2",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    versions = UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")
    assert len(versions) == 3
    active, superseded = active_versions(versions)
    assert {v.seq for v in active} == {1, 3}
    assert superseded == {2}


# ── Day-detail template surface ───────────────────────────────────


def test_day_detail_shows_reassignment_form_for_trip_day(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "i@x.test")
    r = client.get("/day/2026-06-02")
    assert r.status_code == 200
    body = r.text
    assert "Reassign / record a new version" in body or "Correcting" in body
    assert 'name="entry_mode"' in body
    assert "Simple" in body and "Detailed" in body


def test_day_detail_renders_history_after_save(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "j@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "6.08",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02")
    body = r.text
    assert "Assignment history" in body
    assert "722/754" in body
    assert "Pilot reassignment" in body
    assert "6.08" in body


def test_day_detail_pre_fills_for_correction(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "k@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "5.30",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    r = client.get("/day/2026-06-02?correct=1")
    body = r.text
    assert "Correcting v1" in body
    # Hidden version_type and correction_of are wired
    assert 'name="version_type"' in body and 'value="CORRECTION"' in body
    assert 'name="correction_of"' in body and 'value="1"' in body


# ── hard delete of a version (route) ─────────────────────────────────


def test_version_delete_route_removes_and_cascades(monkeypatch):
    """POST /day/<d>/version/<seq>/delete hard-removes the row and any
    correction of it; the page redirects with saved=version_deleted."""
    client, uid = _bootstrap_user_with_june(monkeypatch, "del-a@x.test")
    # v1 reassignment, v2 corrects v1.
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "7.09",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "CORRECTION", "correction_of": "1",
              "entry_mode": "SIMPLE", "assignment_id": "722/754",
              "pch_value": "6.08", "reason_code": "REASSIGNMENT",
              "premium_category": "NONE"},
        follow_redirects=False,
    )
    assert len(UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02")) == 2

    r = client.post("/day/2026-06-02/version/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/day/2026-06-02?saved=version_deleted"
    # Cascade: deleting v1 also removed the correction v2.
    assert UserAssignmentVersionStore(user_id=uid).list_for_date("2026-06-02") == []


def test_version_delete_blocks_default_user():
    client = TestClient(app)  # AUTH_REQUIRED unset → default user
    r = client.post("/day/2026-06-02/version/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "Default%20user" in r.headers["location"]


def test_version_delete_rejects_seq_zero(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "del-b@x.test")
    r = client.post("/day/2026-06-02/version/0/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "reassign_error" in r.headers["location"]


def test_version_delete_shows_banner_and_button(monkeypatch):
    client, _ = _bootstrap_user_with_june(monkeypatch, "del-c@x.test")
    client.post(
        "/day/2026-06-02/reassign",
        data={"version_type": "REASSIGNMENT", "entry_mode": "SIMPLE",
              "assignment_id": "722/754", "pch_value": "6.08",
              "reason_code": "REASSIGNMENT", "premium_category": "NONE"},
        follow_redirects=False,
    )
    # The history renders a Delete control for the user version.
    body = client.get("/day/2026-06-02").text
    assert "/day/2026-06-02/version/1/delete" in body
    # The post-delete confirmation banner renders on the saved redirect target.
    banner = client.get("/day/2026-06-02?saved=version_deleted").text
    assert "Version deleted." in banner
