"""Billing routes — /billing status page + stub upgrade endpoint.

Phase B1 lands the status page and a stub for /billing/upgrade so the
gating middleware works end-to-end. Phase B2 replaces the stub with a
real Stripe Checkout session and the webhook handler.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required
from nac_pay.billing import snapshot

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


@router.get("/billing", response_class=HTMLResponse)
def billing_status(request: Request) -> HTMLResponse:
    """Status + upgrade page. Public-to-this-middleware so an expired
    user can still reach it to recover."""
    if not auth_required():
        # Dev / default user case — render a friendly placeholder.
        snap = None
    else:
        user_id = request.session.get("user_id")
        snap = snapshot(user_id) if user_id else None
    return _TEMPLATES.TemplateResponse(
        request,
        "billing.html",
        {"snapshot": snap, "active_screen": "billing"},
    )


@router.post("/billing/upgrade")
def billing_upgrade(request: Request) -> RedirectResponse:
    """Stub. Phase B2 swaps this for a real Stripe Checkout Session."""
    return RedirectResponse(
        "/billing?stripe_coming_soon=1", status_code=303,
    )
