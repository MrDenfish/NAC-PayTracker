"""Progressive-Web-App layer: service worker, manifest, and the per-user
offline pre-warm URL list.

The app is a thin server-rendered client (all navigation is plain full-page
GETs; every expander is client-side ``<details>``/CSS), so a read-only
service-worker cache makes the whole app browsable offline. Pilots fly to
remote stations without coverage and want to review already-synced pay.

Scope constraint: a service worker only controls pages at or below its own
URL path, so ``/sw.js`` (and the manifest, for a clean root install scope)
are served from the site ROOT here rather than from ``/static/``.

The routes are additive — no existing route or engine behaviour changes.
"""

from __future__ import annotations

import calendar
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from nac_pay.auth import auth_required as _auth_required_flag
from nac_pay.storage.users import DEFAULT_USER_ID

from .services import available_months
from .static_version import STATIC_VERSION

_HERE = Path(__file__).resolve().parent
_SW_SRC = _HERE / "static" / "sw.js"

router = APIRouter()

# Theme colour reused by the manifest and the ``<meta name="theme-color">``.
THEME_COLOR = "#0f172a"  # slate-900, matches the app's dark topbar


def _user_id(request: Request) -> str:
    """Resolve the active user from the session (mirrors ``main._user_id``).

    Duplicated deliberately: ``main`` imports this module to mount the
    router, so importing back from ``main`` would be circular.
    """
    if not _auth_required_flag():
        return DEFAULT_USER_ID
    return request.session.get("user_id") or DEFAULT_USER_ID


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    """Serve the service worker from the site root.

    ``Service-Worker-Allowed: /`` lets a script physically served here
    control the whole origin. ``Cache-Control: no-cache`` keeps Cloudflare
    from pinning a stale worker at the edge (the same per-POP cache gotcha
    the CSS ``?v=<hash>`` busting was added for). The ``__CACHE_VERSION__``
    placeholder is replaced with the static-asset hash so a deploy that
    changes any bundled asset rotates the cache name and evicts the old one.
    """
    body = _SW_SRC.read_text(encoding="utf-8").replace(
        "__CACHE_VERSION__", STATIC_VERSION
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> Response:
    """Web app manifest, served from root so the install scope is the whole
    origin. Explicit ``scope``/``start_url`` == ``/`` make that unambiguous."""
    data = {
        "name": "NAC Pay Tracker",
        "short_name": "NAC Pay",
        "description": "Independent JCBA-2019 §3 pay tracker for NAC pilots.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {
                "src": f"/static/icons/icon-192.png?v={STATIC_VERSION}",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"/static/icons/icon-512.png?v={STATIC_VERSION}",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"/static/icons/icon-maskable-512.png?v={STATIC_VERSION}",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JSONResponse(
        content=data,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/offline-manifest.json", include_in_schema=False)
def offline_manifest(request: Request) -> JSONResponse:
    """The per-user list of URLs to pre-warm into the offline cache.

    Built from the months this user actually has data for
    (``available_months``): the five month-scoped views plus every
    day-detail page for each month, plus the two month-less pages. The
    browser fetches this while online and hands it to the service worker.
    """
    uid = _user_id(request)
    urls: list[str] = ["/settings", "/documents"]
    for year, month, _label in available_months(uid):
        ym = f"{year}-{month}"
        urls.append(f"/?ym={ym}")
        for view in ("calendar", "pay", "compare", "discrepancies"):
            urls.append(f"/{view}?ym={ym}")
        _first_weekday, days_in_month = calendar.monthrange(year, month)
        for day in range(1, days_in_month + 1):
            urls.append(f"/day/{year:04d}-{month:02d}-{day:02d}")
    return JSONResponse(
        content={"version": STATIC_VERSION, "urls": urls},
        headers={"Cache-Control": "no-cache"},
    )
