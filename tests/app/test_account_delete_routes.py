"""Delete-account routes: Danger Zone link, password + DELETE confirm gate,
happy-path purge + session end."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.onboarding import mark_completed
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

_PASSWORD = "long enough password"


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    client.post(
        "/signup",
        data={"email": email, "password": _PASSWORD, "confirm": _PASSWORD},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    uid = find_by_email(email)
    assert uid is not None
    # Bypass the onboarding wizard redirect so /account/delete is reachable —
    # this test exercises deletion, not the onboarding flow.
    mark_completed(uid)
    return uid


def test_delete_requires_correct_password_and_word(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    email = "wilma@example.com"
    _signup_and_verify(client, email)

    # Wrong password, correct confirm word.
    r = client.post(
        "/account/delete",
        data={"password": "totally wrong", "confirm": "DELETE"},
    )
    assert r.status_code == 200
    assert "password" in r.text.lower()
    assert find_by_email(email) is not None

    # Correct password, wrong confirm word.
    r = client.post(
        "/account/delete",
        data={"password": _PASSWORD, "confirm": "delete me"},
    )
    assert r.status_code == 200
    assert find_by_email(email) is not None


def test_delete_happy_path_removes_user_and_ends_session(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    email = "fred@example.com"
    _signup_and_verify(client, email)

    r = client.post(
        "/account/delete",
        data={"password": _PASSWORD, "confirm": "DELETE"},
    )
    assert r.status_code == 200
    assert "Your account and all of its data have been deleted" in r.text
    assert find_by_email(email) is None

    # Session is gone: "/" serves the public landing page, and protected
    # pages redirect to /login.
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "private pay-tracking tool" in r.text
    r = client.get("/calendar", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_delete_reachable_and_completes_with_expired_trial(monkeypatch):
    """An expired-trial user (no active subscription — SubscriptionRequired-
    Middleware would otherwise redirect everything to /billing) must still
    be able to reach and complete account deletion. Expiry simulated the
    same way tests/billing/test_routes.py does: push trial_ends_at into the
    past — snapshot() computes TRIAL_EXPIRED from that at read time, so
    has_access() is False without touching the persisted status column."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    email = "expired@example.com"
    uid = _signup_and_verify(client, email)
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.trial_ends_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="seconds")

    r = client.get("/account/delete", follow_redirects=False)
    assert r.status_code == 200
    assert "Delete your account" in r.text

    r = client.post(
        "/account/delete",
        data={"password": _PASSWORD, "confirm": "DELETE"},
    )
    assert r.status_code == 200
    assert "Your account and all of its data have been deleted" in r.text
    assert find_by_email(email) is None


def test_settings_page_shows_danger_zone():
    client = TestClient(app)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Danger Zone" in r.text
    assert 'href="/account/delete"' in r.text
