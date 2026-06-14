"""ResendEmailSender tests — request shape + env-driven backend selection.

We use httpx's built-in ``MockTransport`` so the sender's outgoing
request is captured for assertions and no actual network call happens.
"""

from __future__ import annotations

import json

import httpx
import pytest

from nac_pay.auth.emails import (
    ConsoleEmailSender,
    ResendEmailSender,
    get_email_sender,
    reset_email_sender,
)


# ── Construction ─────────────────────────────────────────────────────


def test_resend_requires_api_key():
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        ResendEmailSender(api_key="", from_email="x@y.com")


def test_resend_requires_from_email():
    with pytest.raises(RuntimeError, match="RESEND_FROM_EMAIL"):
        ResendEmailSender(api_key="re_test", from_email="")


# ── Request construction ────────────────────────────────────────────


def _capture_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """An httpx.Client whose every request is captured + answered 200."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "msg_abc123"})

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


def test_resend_posts_correct_payload_and_headers():
    client, captured = _capture_client()
    sender = ResendEmailSender(
        api_key="re_test_key", from_email="noreply@nacpay.test", client=client,
    )
    sender.send("alice@example.com", "Verify your email", "Click here: ...")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == ResendEmailSender.ENDPOINT
    assert req.headers["Authorization"] == "Bearer re_test_key"
    assert req.headers["Content-Type"].startswith("application/json")

    payload = json.loads(req.content)
    assert payload == {
        "from": "noreply@nacpay.test",
        "to": ["alice@example.com"],
        "subject": "Verify your email",
        "text": "Click here: ...",
    }


def test_resend_raises_on_4xx():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"name": "invalid_email"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = ResendEmailSender(
        api_key="re_test_key", from_email="noreply@nacpay.test", client=client,
    )
    with pytest.raises(RuntimeError, match="Resend send failed 422"):
        sender.send("bad@", "subject", "body")


def test_resend_raises_on_5xx():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = ResendEmailSender(
        api_key="re_test_key", from_email="noreply@nacpay.test", client=client,
    )
    with pytest.raises(RuntimeError, match="Resend send failed 503"):
        sender.send("ok@example.com", "subject", "body")


# ── Env-driven backend selection ────────────────────────────────────


def test_get_email_sender_console_by_default():
    reset_email_sender()
    assert isinstance(get_email_sender(), ConsoleEmailSender)


def test_get_email_sender_resend_when_env_set(monkeypatch):
    reset_email_sender()
    monkeypatch.setenv("EMAIL_BACKEND", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "noreply@nacpay.test")
    sender = get_email_sender()
    assert isinstance(sender, ResendEmailSender)


def test_get_email_sender_resend_fails_without_api_key(monkeypatch):
    reset_email_sender()
    monkeypatch.setenv("EMAIL_BACKEND", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("RESEND_FROM_EMAIL", "noreply@nacpay.test")
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        get_email_sender()


def test_get_email_sender_resend_fails_without_from_email(monkeypatch):
    reset_email_sender()
    monkeypatch.setenv("EMAIL_BACKEND", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="RESEND_FROM_EMAIL"):
        get_email_sender()


def test_get_email_sender_unknown_backend_raises(monkeypatch):
    reset_email_sender()
    monkeypatch.setenv("EMAIL_BACKEND", "sendgrid")
    with pytest.raises(ValueError, match="EMAIL_BACKEND='sendgrid'"):
        get_email_sender()
