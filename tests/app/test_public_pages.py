"""Public trust pages: landing at ``/``, ``/privacy``, ``/terms``, and
``/robots.txt`` — all reachable without a session when AUTH_REQUIRED=true.

Added after the 2026-08 Bitdefender phishing false-positive: a young
domain that shows strangers nothing but a credential form reads as
phishing to reputation scanners. Anonymous visitors now get a real
landing page; the dashboard behavior for signed-in users is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app

# Distinctive landing-page copy the tests key on.
LANDING_MARKER = "private pay-tracking tool"


@pytest.fixture()
def anon(monkeypatch) -> TestClient:
    """Client with auth ON and no session — what a stranger (or a
    reputation crawler) sees."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    return TestClient(app)


def test_anonymous_root_renders_landing(anon):
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert LANDING_MARKER in r.text
    assert 'href="/login"' in r.text
    assert 'href="/privacy"' in r.text
    assert 'href="/terms"' in r.text
    assert "support@pch-ledger.com" in r.text


def test_landing_does_not_leak_dashboard_data(anon):
    # The dashboard route resolves a default user id when no session is
    # present; the landing branch must fire BEFORE any user resolution so
    # bundled sample data (owner name) never renders for strangers.
    r = anon.get("/", follow_redirects=False)
    assert "Dennis FISHER" not in r.text


def test_landing_does_not_register_service_worker(anon):
    # Public pages must not register the SW: an anonymous visit should
    # never trigger the ~110-page offline pre-warm (pageview amplifier
    # in the 2026-08 abuse incident).
    r = anon.get("/")
    assert "serviceWorker" not in r.text


def test_landing_ignores_month_query_params(anon):
    r = anon.get("/?ym=2026-8", follow_redirects=False)
    assert r.status_code == 200
    assert LANDING_MARKER in r.text


@pytest.mark.parametrize("path,marker", [("/privacy", "Privacy"), ("/terms", "Terms")])
def test_privacy_and_terms_public(anon, path, marker):
    r = anon.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert marker in r.text


def test_robots_txt(anon):
    r = anon.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in r.text
    assert "Disallow: /verify/" in r.text
    assert "Disallow: /reset/" in r.text


@pytest.mark.parametrize("path", ["/calendar", "/pay", "/settings", "/documents"])
def test_protected_pages_still_redirect(anon, path):
    r = anon.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_auth_pages_link_privacy_and_terms(anon):
    for path in ("/login", "/signup"):
        r = anon.get(path)
        assert 'href="/privacy"' in r.text, path
        assert 'href="/terms"' in r.text, path
