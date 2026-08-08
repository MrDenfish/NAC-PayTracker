"""Turnstile gating on the signup + forgot routes.

Uses the ``fake`` backend: only the magic token ``"pass"`` verifies.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.auth import email_exists, get_email_sender

client = TestClient(app)

SIGNUP_FORM = {
    "email": "turnstile-test@example.com",
    "password": "a strong password",
    "confirm": "a strong password",
}


@pytest.fixture()
def fake_turnstile(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "fake")
    from nac_pay.auth.turnstile import get_turnstile

    return get_turnstile()


# ── Widget rendering ─────────────────────────────────────────────────


def test_signup_page_renders_widget_when_enabled(fake_turnstile):
    r = client.get("/signup")
    assert r.status_code == 200
    assert 'class="cf-turnstile"' in r.text
    assert 'data-sitekey="fake-site-key"' in r.text
    assert "challenges.cloudflare.com/turnstile/v0/api.js" in r.text


def test_forgot_page_renders_widget_when_enabled(fake_turnstile):
    r = client.get("/forgot")
    assert r.status_code == 200
    assert 'class="cf-turnstile"' in r.text
    assert 'data-sitekey="fake-site-key"' in r.text


def test_signup_page_has_no_widget_when_disabled():
    r = client.get("/signup")
    assert r.status_code == 200
    assert "cf-turnstile" not in r.text


# ── Signup gating ────────────────────────────────────────────────────


def test_signup_rejected_without_token(fake_turnstile):
    r = client.post("/signup", data=SIGNUP_FORM, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert not email_exists(SIGNUP_FORM["email"])
    assert get_email_sender().sent == []


def test_signup_rejected_with_bad_token(fake_turnstile):
    r = client.post(
        "/signup",
        data={**SIGNUP_FORM, "cf-turnstile-response": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert not email_exists(SIGNUP_FORM["email"])
    assert get_email_sender().sent == []


def test_signup_proceeds_with_valid_token(fake_turnstile):
    r = client.post(
        "/signup",
        data={**SIGNUP_FORM, "cf-turnstile-response": "pass"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/signup?sent=1"
    assert email_exists(SIGNUP_FORM["email"])
    assert len(get_email_sender().sent) == 1


def test_signup_passes_client_ip_to_siteverify(fake_turnstile):
    client.post(
        "/signup",
        data={**SIGNUP_FORM, "cf-turnstile-response": "pass"},
        headers={"CF-Connecting-IP": "203.0.113.5"},
        follow_redirects=False,
    )
    assert fake_turnstile.attempts[-1].remote_ip == "203.0.113.5"


def test_signup_still_works_when_backend_disabled():
    r = client.post("/signup", data=SIGNUP_FORM, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/signup?sent=1"
    assert email_exists(SIGNUP_FORM["email"])


# ── Forgot gating ────────────────────────────────────────────────────


def _create_verified_user(email: str) -> None:
    client.post(
        "/signup",
        data={"email": email, "password": "a strong password",
              "confirm": "a strong password", "cf-turnstile-response": "pass"},
        follow_redirects=False,
    )
    get_email_sender().clear()


def test_forgot_rejected_with_bad_token(fake_turnstile):
    _create_verified_user("dave@example.com")
    r = client.post(
        "/forgot",
        data={"email": "dave@example.com", "cf-turnstile-response": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert get_email_sender().sent == []


def test_forgot_proceeds_with_valid_token(fake_turnstile):
    _create_verified_user("erin@example.com")
    r = client.post(
        "/forgot",
        data={"email": "erin@example.com", "cf-turnstile-response": "pass"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/forgot?sent=1"
    assert len(get_email_sender().sent) == 1
