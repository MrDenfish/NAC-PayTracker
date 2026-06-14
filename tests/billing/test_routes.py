"""End-to-end tests for the trial lifecycle: signup → trial activated on
verify → middleware gates expired users → /billing reachable when expired."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import get_email_sender
from nac_pay.billing import STATUS_TRIAL_EXPIRED, STATUS_TRIALING, snapshot
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _extract_verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m, f"no token in {body!r}"
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str, password: str = "long enough password") -> str:
    """Helper: complete the full signup+verify flow and return the user_id."""
    client.post(
        "/signup",
        data={"email": email, "password": password, "confirm": password},
        follow_redirects=False,
    )
    token = _extract_verify_token(get_email_sender().sent[-1].body)
    r = client.get(f"/verify/{token}", follow_redirects=False)
    assert r.status_code == 200
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.email == email.lower())
        ).scalar_one()
        return row.user_id


def _expire_trial(user_id: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one()
        row.trial_ends_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(timespec="seconds")


client = TestClient(app)


# ── Trial activation hook ─────────────────────────────────────────────


def test_email_verification_starts_a_trial():
    uid = _signup_and_verify(client, "alice@example.com")
    snap = snapshot(uid)
    assert snap.status == STATUS_TRIALING
    assert snap.days_left_in_trial >= 89


# ── Middleware gating ────────────────────────────────────────────────


def test_authenticated_trialing_user_can_reach_dashboard(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "bob@example.com")
    r = isolated.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_expired_trial_redirects_to_billing(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "carol@example.com")
    _expire_trial(uid)
    r = isolated.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/billing"


def test_billing_page_reachable_even_when_expired(monkeypatch):
    """Expired users must be able to reach /billing to recover."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "dave@example.com")
    _expire_trial(uid)
    r = isolated.get("/billing", follow_redirects=False)
    assert r.status_code == 200
    assert "Your free trial has ended" in r.text


def test_logout_works_even_when_expired(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "eve@example.com")
    _expire_trial(uid)
    r = isolated.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_default_user_bypasses_billing_gate():
    """In dev mode (AUTH_REQUIRED=false), the bundled default user always
    has access — no subscription record needed."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Dennis FISHER" in r.text


def test_static_paths_remain_open_when_expired(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "frank@example.com")
    _expire_trial(uid)
    r = isolated.get("/static/styles.css", follow_redirects=False)
    assert r.status_code == 200


# ── /billing status page ────────────────────────────────────────────


def test_billing_shows_trial_remaining_for_active_trial(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "greg@example.com")
    r = isolated.get("/billing")
    assert r.status_code == 200
    assert "Free trial active" in r.text
    # Either "89 days left" or "90 days left" depending on test execution timing
    assert "day" in r.text


def test_billing_upgrade_stub_redirects_back_with_coming_soon(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "hank@example.com")
    r = isolated.post("/billing/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert "stripe_coming_soon=1" in r.headers["location"]
