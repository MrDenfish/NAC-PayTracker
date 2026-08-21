"""Public trust pages: landing, privacy, terms, robots.txt.

Added after the 2026-08 Bitdefender phishing false-positive. A young
domain that shows strangers nothing but a credential form matches the
textbook phishing heuristic; these pages give anonymous visitors (and
reputation crawlers) benign content, a policy trail, and a contact
address. None of them touch user data or register the service worker.

The landing page itself is returned by the ``/`` route in ``main`` (the
route stays owned by the dashboard; it branches to ``landing_response``
for sessionless visitors) — this module serves the rest and owns the
shared template environment for the ``public/`` templates.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()

# Keep crawlers away from the tokenized email-link paths — those URLs
# showing up in crawls/reports is part of what got the domain flagged.
_ROBOTS_TXT = """\
User-agent: *
Allow: /
Disallow: /verify/
Disallow: /reset/
"""


def landing_response(request: Request) -> HTMLResponse:
    """The anonymous-visitor landing page for ``/``."""
    return _TEMPLATES.TemplateResponse(request, "public/landing.html", {})


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "public/privacy.html", {})


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "public/terms.html", {})


@router.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(_ROBOTS_TXT)
