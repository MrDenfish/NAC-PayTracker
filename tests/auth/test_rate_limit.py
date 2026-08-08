"""Per-IP rate limiting on the auth endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.auth import email_exists, get_email_sender
from nac_pay.auth.rate_limit import RateLimiter

client = TestClient(app)


# ── RateLimiter unit ─────────────────────────────────────────────────


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter(limit=3, window_seconds=60)
    assert [rl.allow("k") for _ in range(4)] == [True, True, True, False]


def test_keys_are_independent():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False


def test_window_expiry_allows_again():
    now = [0.0]
    rl = RateLimiter(limit=1, window_seconds=60, clock=lambda: now[0])
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    now[0] = 61.0
    assert rl.allow("k") is True


def test_reset_clears_all_state():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("k") is True
    rl.reset()
    assert rl.allow("k") is True


# ── Route integration ────────────────────────────────────────────────


def _signup(n: int, ip: str = "203.0.113.1"):
    responses = []
    for i in range(n):
        responses.append(
            client.post(
                "/signup",
                data={
                    "email": f"rl-{ip}-{i}@example.com",
                    "password": "a strong password",
                    "confirm": "a strong password",
                },
                headers={"CF-Connecting-IP": ip},
                follow_redirects=False,
            )
        )
    return responses


def test_signup_blocks_sixth_attempt_from_same_ip():
    responses = _signup(6)
    assert [r.status_code for r in responses[:5]] == [303] * 5
    assert responses[5].status_code == 429
    # The blocked attempt did no work: no account, no email.
    assert not email_exists("rl-203.0.113.1-5@example.com")
    assert len(get_email_sender().sent) == 5


def test_signup_limit_is_per_ip():
    _signup(5, ip="203.0.113.2")
    responses = _signup(1, ip="203.0.113.3")
    assert responses[0].status_code == 303


def test_login_blocks_eleventh_attempt_from_same_ip():
    codes = []
    for _ in range(11):
        r = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "wrong password"},
            headers={"CF-Connecting-IP": "203.0.113.4"},
            follow_redirects=False,
        )
        codes.append(r.status_code)
    assert codes[:10] == [303] * 10
    assert codes[10] == 429


def test_forgot_blocks_sixth_attempt_from_same_ip():
    codes = []
    for _ in range(6):
        r = client.post(
            "/forgot",
            data={"email": "nobody@example.com"},
            headers={"CF-Connecting-IP": "203.0.113.5"},
            follow_redirects=False,
        )
        codes.append(r.status_code)
    assert codes[:5] == [303] * 5
    assert codes[5] == 429
