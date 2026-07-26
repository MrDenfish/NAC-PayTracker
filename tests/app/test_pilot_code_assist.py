"""Pilot-code assist on onboarding step 1: live check + find-my-code,
backed by the shared (admin-published) Final Award."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import invalidate_caches, shared_pilot_directory
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.storage import SharedDocumentsStore, get_data_dir

_FA_FIXTURE = "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"
_PACKET_FIXTURE = "MAY  2026  Trip Pairing Packet.pdf"

# Known from the bundled fixture (see services.DEFAULT_PILOT / other tests
# that load the default pilot against this same FA).
_KNOWN_CODE = "DFI"
_KNOWN_LASTNAME_PREFIX = "fish"


def _fixture(name: str) -> bytes:
    return (Path(__file__).resolve().parents[2] / "docs" / name).read_bytes()


def _publish_shared(year: int, month: int) -> None:
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_packet(year, month, "packet-shared.pdf", _fixture(_PACKET_FIXTURE), uploaded_by="admin")
    invalidate_caches()


def _publish_shared_current_month() -> tuple[int, int]:
    today = date.today()
    _publish_shared(today.year, today.month)
    return today.year, today.month


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    """Signs up + verifies a user but deliberately does NOT mark onboarding
    completed — the pilot-code assist is exercised mid-onboarding."""
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    uid = find_by_email(email)
    assert uid is not None
    from sqlalchemy import select

    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserRow
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.subscription_status = "ACTIVE"
    return uid


# ── services.shared_pilot_directory ─────────────────────────────────


def test_shared_pilot_directory_prefers_current_then_falls_back():
    _publish_shared(2026, 5)
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert "May 2026" in label
    assert all(len(code) <= 4 for code in directory)
    assert directory


def test_shared_pilot_directory_empty_when_nothing_published():
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert label == ""
    assert directory == {}


def test_shared_pilot_directory_future_only_still_used():
    # Launch scenario: only NEXT month's FA has been published yet (no
    # current or past month). Codes are stable, so this is still a valid
    # check — the assist should use the nearest future month rather than
    # going empty.
    _publish_shared(2026, 9)
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert "September 2026" in label
    assert directory


def test_shared_pilot_directory_prefers_current_over_older():
    _publish_shared(2026, 5)   # older
    _publish_shared(2026, 7)   # current
    label, directory = shared_pilot_directory(today=date(2026, 7, 15))
    assert "July 2026" in label
    assert directory


# ── GET /onboarding/code-lookup ──────────────────────────────────────


def test_code_lookup_endpoint_by_code_and_lastname(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "quincy@example.com")

    r = client.get(f"/onboarding/code-lookup?code={_KNOWN_CODE}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["code"] == _KNOWN_CODE

    r2 = client.get(f"/onboarding/code-lookup?last_name={_KNOWN_LASTNAME_PREFIX}")
    assert r2.status_code == 200
    assert len(r2.json()["matches"]) >= 1

    r3 = client.get("/onboarding/code-lookup?code=ZZZ")
    assert r3.status_code == 200
    assert r3.json()["matches"] == []


def test_code_lookup_empty_when_no_shared_fa(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    _signup_and_verify(client, "rex@example.com")

    r = client.get("/onboarding/code-lookup?code=ZZZ")
    assert r.status_code == 200
    assert r.json() == {"month_label": "", "matches": []}


# ── POST /onboarding/profile warn-once gate ──────────────────────────


def test_profile_post_warns_once_then_accepts(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "sara@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "Sara Pilot",
            "pilot_id": "ZZZ",
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "not found" in r.text
    assert 'name="confirmed_code" value="ZZZ"' in r.text
    # The typed values are preserved, not reset to the persisted defaults.
    assert 'value="Sara Pilot"' in r.text

    r2 = client.post(
        "/onboarding/profile",
        data={
            "name": "Sara Pilot",
            "pilot_id": "ZZZ",
            "position": "FO",
            "hourly_rate": "130.00",
            "confirmed_code": "ZZZ",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers["location"] == "/onboarding/feed"


def test_profile_post_known_code_passes_straight_through(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    _publish_shared_current_month()
    client = TestClient(app)
    _signup_and_verify(client, "tom@example.com")

    r = client.post(
        "/onboarding/profile",
        data={
            "name": "Tom Pilot",
            "pilot_id": _KNOWN_CODE,
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/feed"
