"""Email sending — pluggable backend.

The protocol-level abstraction lets us run ``ConsoleEmailSender`` in dev
(prints to stdout, captured for tests) and ``ResendEmailSender`` in
production (POSTs to https://api.resend.com/emails). Backend selected by
``EMAIL_BACKEND`` env var (``console`` / ``resend``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Dev backend: prints the email to stdout and stores it for assertions."""

    def __init__(self) -> None:
        self.sent: list[SentEmail] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append(SentEmail(to=to, subject=subject, body=body))
        print(
            f"\n[ConsoleEmailSender] to={to}\n"
            f"subject: {subject}\n"
            f"{body}\n",
            flush=True,
        )

    def clear(self) -> None:
        self.sent.clear()


class ResendEmailSender:
    """Production email backend — POSTs to the Resend HTTP API.

    Requires two env vars:
    - ``RESEND_API_KEY`` (bearer token from resend.com dashboard)
    - ``RESEND_FROM_EMAIL`` (the verified-domain sender, e.g.
      ``noreply@nacpay.app``)

    The ``timeout`` defaults to 10 seconds — Resend's API is typically
    sub-second; anything longer means we'd rather surface an error to
    the user than block their signup.
    """

    ENDPOINT = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        from_email: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ):
        if not api_key:
            raise RuntimeError("ResendEmailSender requires RESEND_API_KEY")
        if not from_email:
            raise RuntimeError("ResendEmailSender requires RESEND_FROM_EMAIL")
        self._api_key = api_key
        self._from_email = from_email
        self._timeout = timeout
        # Allow injection for tests; default client created lazily so
        # importing the module doesn't open a TCP connection.
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def send(self, to: str, subject: str, body: str) -> None:
        response = self._http().post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": self._from_email,
                "to": [to],
                "subject": subject,
                "text": body,
            },
        )
        if response.status_code >= 400:
            # Surface the failure — silent loss is worse than a 500 for
            # the user, who can retry.
            raise RuntimeError(
                f"Resend send failed {response.status_code}: {response.text}"
            )


_SENDER: EmailSender | None = None


def get_email_sender() -> EmailSender:
    """Resolve the configured email backend. Cached per-process (lazy)."""
    global _SENDER
    if _SENDER is None:
        backend = os.environ.get("EMAIL_BACKEND", "console").lower()
        if backend == "console":
            _SENDER = ConsoleEmailSender()
        elif backend == "resend":
            _SENDER = ResendEmailSender(
                api_key=os.environ.get("RESEND_API_KEY", ""),
                from_email=os.environ.get("RESEND_FROM_EMAIL", ""),
            )
        else:
            raise ValueError(f"Unknown EMAIL_BACKEND={backend!r}")
    return _SENDER


def reset_email_sender() -> None:
    """Test helper: drop the cached sender so the next call rebuilds it
    with current env settings."""
    global _SENDER
    _SENDER = None


# ── Common email templates ────────────────────────────────────────────


def base_url() -> str:
    return os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def send_verification_email(to: str, token: str) -> None:
    link = f"{base_url()}/verify/{token}"
    body = (
        "Welcome to NAC Pay Tracker.\n\n"
        f"Confirm your email by visiting:\n  {link}\n\n"
        "This link expires in 24 hours. If you didn't sign up, ignore this email."
    )
    get_email_sender().send(to=to, subject="Verify your email", body=body)


def send_password_reset_email(to: str, token: str) -> None:
    link = f"{base_url()}/reset/{token}"
    body = (
        "We received a request to reset your NAC Pay Tracker password.\n\n"
        f"Reset it by visiting:\n  {link}\n\n"
        "This link expires in 1 hour. If you didn't request this, ignore this email."
    )
    get_email_sender().send(to=to, subject="Reset your password", body=body)
