"""Cloudflare Turnstile verification — pluggable backend.

Mirrors the ``emails.py`` pattern: ``DisabledTurnstile`` in dev (no
widget rendered, every submit passes), ``FakeTurnstile`` in tests
(passes only the magic token ``"pass"``), ``CloudflareTurnstile`` in
production (canonical server-side siteverify POST). Backend selected by
``TURNSTILE_BACKEND`` env var (``off`` / ``fake`` / ``cloudflare``).

Verification FAILS CLOSED: any siteverify network error, HTTP error, or
malformed body counts as "not verified" — a bot must never get through
because Cloudflare had a hiccup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class VerifyAttempt:
    token: str
    remote_ip: str | None


class TurnstileVerifier(Protocol):
    enabled: bool
    site_key: str

    def verify(self, token: str, remote_ip: str | None = None) -> bool: ...


class DisabledTurnstile:
    """Dev backend: no widget, everything verifies."""

    enabled = False
    site_key = ""

    def verify(self, token: str, remote_ip: str | None = None) -> bool:
        return True


class FakeTurnstile:
    """Test backend: passes only the magic token ``"pass"``; records calls."""

    enabled = True
    site_key = "fake-site-key"

    def __init__(self) -> None:
        self.attempts: list[VerifyAttempt] = []

    def verify(self, token: str, remote_ip: str | None = None) -> bool:
        self.attempts.append(VerifyAttempt(token=token, remote_ip=remote_ip))
        return token == "pass"


class CloudflareTurnstile:
    """Production backend — canonical siteverify POST."""

    ENDPOINT = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    enabled = True

    def __init__(
        self,
        site_key: str,
        secret: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ):
        if not site_key:
            raise RuntimeError("CloudflareTurnstile requires TURNSTILE_SITE_KEY")
        if not secret:
            raise RuntimeError("CloudflareTurnstile requires TURNSTILE_SECRET")
        self.site_key = site_key
        self._secret = secret
        self._timeout = timeout
        # Allow injection for tests; default client created lazily so
        # importing the module doesn't open a TCP connection.
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def verify(self, token: str, remote_ip: str | None = None) -> bool:
        if not token:
            return False
        data = {"secret": self._secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            response = self._http().post(self.ENDPOINT, data=data)
            if response.status_code >= 400:
                return False
            return response.json().get("success") is True
        except (httpx.HTTPError, ValueError):
            return False


_VERIFIER: TurnstileVerifier | None = None


def get_turnstile() -> TurnstileVerifier:
    """Resolve the configured Turnstile backend. Cached per-process (lazy)."""
    global _VERIFIER
    if _VERIFIER is None:
        backend = os.environ.get("TURNSTILE_BACKEND", "off").lower()
        if backend == "off":
            _VERIFIER = DisabledTurnstile()
        elif backend == "fake":
            _VERIFIER = FakeTurnstile()
        elif backend == "cloudflare":
            _VERIFIER = CloudflareTurnstile(
                site_key=os.environ.get("TURNSTILE_SITE_KEY", ""),
                secret=os.environ.get("TURNSTILE_SECRET", ""),
            )
        else:
            raise ValueError(f"Unknown TURNSTILE_BACKEND={backend!r}")
    return _VERIFIER


def reset_turnstile() -> None:
    """Test helper: drop the cached verifier so the next call rebuilds it
    with current env settings."""
    global _VERIFIER
    _VERIFIER = None
