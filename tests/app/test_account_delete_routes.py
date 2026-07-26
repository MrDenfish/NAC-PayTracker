"""Delete-account routes: Danger Zone link, password + DELETE confirm gate,
happy-path purge + session end."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.auth import find_by_email, get_email_sender
from nac_pay.onboarding import mark_completed

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

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_settings_page_shows_danger_zone():
    client = TestClient(app)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Danger Zone" in r.text
    assert 'href="/account/delete"' in r.text
