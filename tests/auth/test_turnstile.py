"""Turnstile verifier — pluggable backend, canonical siteverify, fail closed."""

from __future__ import annotations

import json

import httpx
import pytest

from nac_pay.auth.turnstile import (
    CloudflareTurnstile,
    DisabledTurnstile,
    FakeTurnstile,
    get_turnstile,
    reset_turnstile,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_turnstile()
    yield
    reset_turnstile()


# ── Disabled backend (dev default) ───────────────────────────────────


def test_disabled_backend_verifies_everything():
    t = DisabledTurnstile()
    assert t.enabled is False
    assert t.site_key == ""
    assert t.verify("", None) is True
    assert t.verify("anything", "1.2.3.4") is True


# ── Fake backend (tests) ─────────────────────────────────────────────


def test_fake_backend_passes_only_the_magic_token():
    t = FakeTurnstile()
    assert t.enabled is True
    assert t.verify("pass", "1.2.3.4") is True
    assert t.verify("wrong", None) is False
    assert [a.token for a in t.attempts] == ["pass", "wrong"]


# ── Cloudflare backend ───────────────────────────────────────────────


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cloudflare_posts_canonical_siteverify_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"success": True})

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("tok-123", "203.0.113.9") is True
    assert seen["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert "secret=sekrit" in seen["body"]
    assert "response=tok-123" in seen["body"]
    assert "remoteip=203.0.113.9" in seen["body"]


def test_cloudflare_rejects_when_success_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("bad-token", None) is False


def test_cloudflare_fails_closed_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("tok", None) is False


def test_cloudflare_fails_closed_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("tok", None) is False


def test_cloudflare_fails_closed_on_non_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>edge error</html>")

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("tok", None) is False


def test_cloudflare_rejects_empty_token_without_calling_api():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("siteverify must not be called for an empty token")

    t = CloudflareTurnstile("site-key", "sekrit", client=_client(handler))
    assert t.verify("", "1.2.3.4") is False


# ── Backend selection ────────────────────────────────────────────────


def test_default_backend_is_disabled(monkeypatch):
    monkeypatch.delenv("TURNSTILE_BACKEND", raising=False)
    assert isinstance(get_turnstile(), DisabledTurnstile)


def test_fake_backend_selected_by_env(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "fake")
    assert isinstance(get_turnstile(), FakeTurnstile)


def test_cloudflare_backend_reads_keys_from_env(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "cloudflare")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "0xSITE")
    monkeypatch.setenv("TURNSTILE_SECRET", "0xSECRET")
    t = get_turnstile()
    assert isinstance(t, CloudflareTurnstile)
    assert t.enabled is True
    assert t.site_key == "0xSITE"


def test_cloudflare_backend_requires_keys(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "cloudflare")
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        get_turnstile()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "hcaptcha")
    with pytest.raises(ValueError):
        get_turnstile()


def test_get_turnstile_is_cached_until_reset(monkeypatch):
    monkeypatch.setenv("TURNSTILE_BACKEND", "fake")
    first = get_turnstile()
    assert get_turnstile() is first
    reset_turnstile()
    assert get_turnstile() is not first
