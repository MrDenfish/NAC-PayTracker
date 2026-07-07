"""PWA layer: root-scoped service worker, web manifest, and the per-user
offline pre-warm list. These make the app installable and browsable offline
(read-only). See app/pwa.py, static/sw.js."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import available_months
from nac_pay.app.static_version import STATIC_VERSION
from nac_pay.storage.users import DEFAULT_USER_ID


def _client() -> TestClient:
    # AUTH_REQUIRED unset → every request resolves to the default (dev) user,
    # which reads the bundled docs months.
    return TestClient(app)


def test_service_worker_served_from_root_with_scope_header():
    r = _client().get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    # Must be allowed to control the whole origin, not just /static/.
    assert r.headers.get("service-worker-allowed") == "/"
    # Keep Cloudflare from pinning a stale worker.
    assert "no-cache" in r.headers.get("cache-control", "")


def test_service_worker_has_version_injected_not_placeholder():
    body = _client().get("/sw.js").text
    assert "__CACHE_VERSION__" not in body, "cache version placeholder not substituted"
    assert STATIC_VERSION in body


def test_manifest_is_root_scoped():
    r = _client().get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(r.text)
    # Root scope + start_url are what make the installed app control the whole
    # origin rather than a subpath.
    assert data["scope"] == "/"
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert any(icon["sizes"] == "512x512" for icon in data["icons"])
    assert any(icon.get("purpose") == "maskable" for icon in data["icons"])


def test_offline_manifest_lists_every_view_for_each_month():
    r = _client().get("/offline-manifest.json")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == STATIC_VERSION
    urls = data["urls"]
    # Month-less pages are always present.
    assert "/settings" in urls
    assert "/documents" in urls

    months = available_months(DEFAULT_USER_ID)
    assert months, "dev user should have bundled months to pre-warm"
    for year, month, _label in months:
        ym = f"{year}-{month}"
        assert f"/?ym={ym}" in urls
        for view in ("calendar", "pay", "compare", "discrepancies"):
            assert f"/{view}?ym={ym}" in urls
        # Each month contributes a full set of day-detail pages.
        assert any(u.startswith(f"/day/{year:04d}-{month:02d}-") for u in urls)


def test_offline_fallback_page_served():
    r = _client().get("/static/offline.html")
    assert r.status_code == 200
    assert "offline" in r.text.lower()
