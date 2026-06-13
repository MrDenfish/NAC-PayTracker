"""Email sending — pluggable backend.

The protocol-level abstraction lets us run ``ConsoleEmailSender`` in dev
(prints to stdout, captured for tests) and a real provider like Resend in
production. Backend selected by ``EMAIL_BACKEND`` env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


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


_SENDER: EmailSender | None = None


def get_email_sender() -> EmailSender:
    """Resolve the configured email backend. Cached per-process (lazy)."""
    global _SENDER
    if _SENDER is None:
        backend = os.environ.get("EMAIL_BACKEND", "console").lower()
        if backend == "console":
            _SENDER = ConsoleEmailSender()
        elif backend == "resend":
            # Production sender lands with the Resend integration commit.
            raise RuntimeError(
                "EMAIL_BACKEND=resend is reserved for Phase C — not implemented yet"
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
