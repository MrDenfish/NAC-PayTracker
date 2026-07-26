"""Onboarding wizard middleware + 3-step flow + skip + completion."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.onboarding import mark_completed, should_onboard
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    uid = find_by_email(email)
    assert uid is not None
    # Promote to ACTIVE so the subscription gate is satisfied; we're
    # specifically testing the onboarding redirect, not billing.
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.subscription_status = "ACTIVE"
    return uid


# ── Middleware redirect ─────────────────────────────────────────────


def test_fresh_user_redirects_from_dashboard_to_onboarding(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "alice@example.com")

    r = isolated.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_default_user_never_redirected_to_onboarding():
    """AUTH_REQUIRED=false → default user, no wizard."""
    client = TestClient(app)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Dennis FISHER" in r.text


def test_completed_user_passes_through_to_dashboard(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "bob@example.com")
    mark_completed(uid)

    r = isolated.get("/", follow_redirects=False)
    # Dashboard renders the empty-state since bob has no documents,
    # but the route did NOT redirect to /onboarding.
    assert r.status_code == 200
    assert "No data for this month yet" in r.text


def test_settings_documents_billing_reachable_during_onboarding(monkeypatch):
    """The wizard isn't a trap — fresh users can still reach Settings,
    Documents, and Billing (they need those to complete setup)."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "carol@example.com")

    assert isolated.get("/settings", follow_redirects=False).status_code == 200
    assert isolated.get("/documents", follow_redirects=False).status_code == 200
    assert isolated.get("/billing", follow_redirects=False).status_code == 200


def test_logout_reachable_during_onboarding(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "dave@example.com")
    r = isolated.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ── /onboarding landing redirects ──────────────────────────────────


def test_onboarding_landing_redirects_fresh_user_to_step_1(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "eve@example.com")
    r = isolated.get("/onboarding", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/profile"


def test_onboarding_landing_sends_completed_user_to_dashboard(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "frank@example.com")
    mark_completed(uid)
    r = isolated.get("/onboarding", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ── Step 1: Profile ────────────────────────────────────────────────


def test_profile_step_saves_pilot_id_and_advances(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "greg@example.com")

    r = isolated.post(
        "/onboarding/profile",
        data={
            "name": "Greg Pilot",
            "pilot_id": "GRG",
            "position": "FO",
            "hourly_rate": "130.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/feed"

    # Profile persisted with the entered pilot_id (uppercased).
    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.profile.pilot_id == "GRG"
    assert p.profile.name == "Greg Pilot"


def test_profile_step_rejects_invalid_position(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "hank@example.com")
    r = isolated.post(
        "/onboarding/profile",
        data={"name": "x", "pilot_id": "HNK", "position": "BAD", "hourly_rate": "100"},
        follow_redirects=False,
    )
    assert "FO+or+CPT" in r.headers["location"]


def test_profile_step_rejects_bad_pilot_id_length(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "ivy@example.com")
    r = isolated.post(
        "/onboarding/profile",
        data={"name": "x", "pilot_id": "ABCDE", "position": "FO", "hourly_rate": "100"},
        follow_redirects=False,
    )
    assert "2-4+letters" in r.headers["location"]


def test_profile_step_button_copy_matches_step_2(monkeypatch):
    """Finding 4: step 2 is the feed-connect step (no upload happens
    there), so the profile page's continue button must say 'Connect
    schedule', not the stale 'Upload month' copy."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "june@example.com")
    r = isolated.get("/onboarding/profile")
    assert r.status_code == 200
    assert "Continue → Connect schedule" in r.text
    assert "Upload month" not in r.text


# ── Step 2: Feed link ──────────────────────────────────────────────


def test_onboarding_feed_saves_url_and_fetches(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "carol@example.com")
    calls = {}

    def fake_update(user_id, url, **kw):
        calls["args"] = (user_id, url)
        from nac_pay.app.feed_updater import UserUpdate
        return UserUpdate(user_id=user_id, months=())

    monkeypatch.setattr("nac_pay.app.onboarding_routes.update_user_feed", fake_update)
    r = client.post(
        "/onboarding/feed",
        data={"feed_url": "https://blueone.example/cal.ics"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/done"
    assert calls["args"] == (uid, "https://blueone.example/cal.ics")
    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.feed_url == "https://blueone.example/cal.ics"
    assert p.feed_auto_update is True


def test_onboarding_feed_rejects_non_http(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "quinn@example.com")
    r = client.post(
        "/onboarding/feed",
        data={"feed_url": "ftp://x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/onboarding/feed?error=")

    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.feed_url == ""


def test_onboarding_feed_fetch_failure_rerenders(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "ray@example.com")

    def fake_update(user_id, url, **kw):
        from nac_pay.app.feed_updater import FeedFetchError
        raise FeedFetchError("boom")

    monkeypatch.setattr("nac_pay.app.onboarding_routes.update_user_feed", fake_update)
    r = client.post(
        "/onboarding/feed",
        data={"feed_url": "https://blueone.example/cal.ics"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/onboarding/feed?error=")

    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.feed_url == ""


def test_onboarding_feed_empty_url_skips(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "sam@example.com")
    r = client.post(
        "/onboarding/feed",
        data={"feed_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/done"

    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.feed_url == ""


def test_old_documents_step_redirects():
    client = TestClient(app)
    r = client.get("/onboarding/documents", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/feed"


# ── Step 3: Done + completion ─────────────────────────────────────


def test_done_step_marks_completed_and_lands_on_dashboard(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "mike@example.com")
    assert should_onboard(uid) is True

    r = isolated.post("/onboarding/done", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert should_onboard(uid) is False


# ── Skip ──────────────────────────────────────────────────────────


def test_skip_marks_completed_and_lands_on_dashboard(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "nora@example.com")
    r = isolated.post("/onboarding/skip", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert should_onboard(uid) is False


# ── Dashboard empty state for completed-but-doc-less users ──────


def test_dashboard_empty_state_when_no_docs(monkeypatch):
    """A user who finished onboarding (or skipped) but uploaded no docs
    sees a friendly empty state, not a 404."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "olive@example.com")
    mark_completed(uid)
    r = isolated.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "No data for this month yet" in r.text
    assert "/documents" in r.text
