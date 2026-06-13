"""End-to-end tests for the signup → verify → login flow and
AUTH_REQUIRED middleware behavior."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.auth import (
    email_exists,
    get_email_sender,
    is_email_verified,
)


def _extract_token(body: str) -> str:
    """Pull a token out of a captured email link."""
    match = re.search(r"/(?:verify|reset)/([A-Za-z0-9_-]+)", body)
    assert match, f"No token in email body: {body!r}"
    return match.group(1)


client = TestClient(app)


# ── Signup → verify ──────────────────────────────────────────────────


def test_signup_creates_pending_user_and_sends_email():
    r = client.post(
        "/signup",
        data={
            "email": "alice@example.com",
            "password": "a strong password",
            "confirm": "a strong password",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/signup?sent=1"
    assert email_exists("alice@example.com")
    # ConsoleEmailSender captured the verification email.
    sent = get_email_sender().sent
    assert len(sent) == 1
    assert sent[0].to == "alice@example.com"
    assert "/verify/" in sent[0].body


def test_signup_rejects_mismatched_passwords():
    r = client.post(
        "/signup",
        data={
            "email": "bob@example.com",
            "password": "long enough one",
            "confirm": "long enough two",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "do+not+match" in r.headers["location"]
    assert not email_exists("bob@example.com")


def test_signup_rejects_short_password():
    r = client.post(
        "/signup",
        data={
            "email": "carol@example.com",
            "password": "short",
            "confirm": "short",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "at+least+10" in r.headers["location"]


def test_signup_rejects_duplicate_email():
    client.post(
        "/signup",
        data={"email": "dave@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    r = client.post(
        "/signup",
        data={"email": "dave@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    assert "already+has+an+account" in r.headers["location"]


def test_verify_link_activates_account_and_signs_in():
    client.post(
        "/signup",
        data={"email": "eve@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    r = client.get(f"/verify/{token}", follow_redirects=False)
    assert r.status_code == 200
    assert "Email verified" in r.text
    # Account is now activated.
    assert is_email_verified_for_email("eve@example.com")


def test_used_verification_token_returns_410():
    client.post(
        "/signup",
        data={"email": "frank@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    first = client.get(f"/verify/{token}")
    assert first.status_code == 200
    second = client.get(f"/verify/{token}")
    assert second.status_code == 410
    assert "already" in second.text or "no longer valid" in second.text


# ── Login ────────────────────────────────────────────────────────────


def test_login_requires_verified_email():
    client.post(
        "/signup",
        data={"email": "greg@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    r = client.post(
        "/login",
        data={"email": "greg@example.com", "password": "long enough p"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "verify+your+email" in r.headers["location"]


def test_login_succeeds_after_verification():
    client.post(
        "/signup",
        data={"email": "hank@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    client.get(f"/verify/{token}")
    # Now sign in fresh in a separate client.
    r = client.post(
        "/login",
        data={"email": "hank@example.com", "password": "long enough p"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_wrong_password_redirects_with_error():
    client.post(
        "/signup",
        data={"email": "ivy@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    client.get(f"/verify/{token}")
    r = client.post(
        "/login",
        data={"email": "ivy@example.com", "password": "wrong password"},
        follow_redirects=False,
    )
    assert "wrong" in r.headers["location"]


# ── Forgot → reset ───────────────────────────────────────────────────


def test_forgot_post_always_redirects_to_sent():
    """Don't reveal whether the email exists — same response either way."""
    r_known = client.post(
        "/forgot", data={"email": "jane@example.com"}, follow_redirects=False
    )
    r_unknown = client.post(
        "/forgot", data={"email": "noone@example.com"}, follow_redirects=False
    )
    assert r_known.headers["location"] == "/forgot?sent=1"
    assert r_unknown.headers["location"] == "/forgot?sent=1"


def test_reset_flow_updates_password():
    client.post(
        "/signup",
        data={"email": "kate@example.com", "password": "old password p", "confirm": "old password p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    client.get(f"/verify/{token}")
    get_email_sender().clear()

    client.post("/forgot", data={"email": "kate@example.com"}, follow_redirects=False)
    reset_token = _extract_token(get_email_sender().sent[0].body)

    r = client.post(
        f"/reset/{reset_token}",
        data={"password": "new password long", "confirm": "new password long"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?reset=1"

    # Old password fails, new password works.
    r_fail = client.post(
        "/login",
        data={"email": "kate@example.com", "password": "old password p"},
        follow_redirects=False,
    )
    assert "wrong" in r_fail.headers["location"]
    r_ok = client.post(
        "/login",
        data={"email": "kate@example.com", "password": "new password long"},
        follow_redirects=False,
    )
    assert r_ok.headers["location"] == "/"


# ── AUTH_REQUIRED behavior ──────────────────────────────────────────


def test_auth_required_false_lets_unauthenticated_dashboard_through():
    """The default for tests is AUTH_REQUIRED unset (= false). The
    dashboard route should respond 200, not redirect."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Dennis FISHER" in r.text


def test_auth_required_true_redirects_unauthenticated_to_login(monkeypatch):
    """When AUTH_REQUIRED=true and no session, dashboard redirects to
    /login. Public paths (login, signup, static) stay open."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    r = isolated.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # /login itself stays reachable
    assert isolated.get("/login").status_code == 200
    assert isolated.get("/signup").status_code == 200
    assert isolated.get("/forgot").status_code == 200


def test_logout_clears_session(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    isolated.post(
        "/signup",
        data={"email": "leo@example.com", "password": "long enough p", "confirm": "long enough p"},
        follow_redirects=False,
    )
    token = _extract_token(get_email_sender().sent[0].body)
    isolated.get(f"/verify/{token}")  # session set
    # Dashboard should be reachable now.
    assert isolated.get("/", follow_redirects=False).status_code == 200
    # Log out.
    r = isolated.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Dashboard redirects again after logout.
    assert isolated.get("/", follow_redirects=False).status_code == 303


# ── Tiny helper used above ───────────────────────────────────────────


def is_email_verified_for_email(email: str) -> bool:
    from nac_pay.auth import find_by_email, is_email_verified
    uid = find_by_email(email)
    return uid is not None and is_email_verified(uid)
