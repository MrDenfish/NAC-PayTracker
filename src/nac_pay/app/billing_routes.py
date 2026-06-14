"""Billing routes — /billing status, /billing/upgrade → Stripe Checkout,
/webhooks/stripe webhook handler.

The Checkout flow:
1. POST /billing/upgrade — server creates a Stripe Customer (idempotent
   by storing customer_id on the user row), then creates a Checkout
   Session, then 303 to the Stripe-hosted URL.
2. User enters card on Stripe's page → Stripe redirects to success_url.
3. Stripe also fires ``checkout.session.completed`` to /webhooks/stripe,
   which is the *authoritative* signal that flips status to ACTIVE.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from nac_pay.auth import auth_required
from nac_pay.billing import (
    apply_stripe_event,
    get_stripe_adapter,
    snapshot,
)
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


def _base_url() -> str:
    return os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _price_id() -> str:
    return os.environ.get("STRIPE_PRICE_ID", "price_test_default")


@router.get("/billing", response_class=HTMLResponse)
def billing_status(request: Request) -> HTMLResponse:
    """Status + upgrade page. Public to the billing gate (recovery path)."""
    if not auth_required():
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
    """Create or fetch the Stripe Customer, create a Checkout Session,
    redirect the user to Stripe's hosted page."""
    if not auth_required():
        return RedirectResponse("/billing?dev_mode=1", status_code=303)
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=303)

    # Read the user's current customer_id + email (cache to avoid round-trip).
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404)
        email = row.email
        existing_customer_id = row.stripe_customer_id

    adapter = get_stripe_adapter()
    customer_id = adapter.create_or_get_customer(email, existing_customer_id)

    # Persist the customer_id so the webhook (which fires by customer_id)
    # can map back to this user row.
    if customer_id != existing_customer_id:
        with session_scope() as sess:
            row = sess.execute(
                select(UserRow).where(UserRow.user_id == user_id)
            ).scalar_one()
            row.stripe_customer_id = customer_id

    base = _base_url()
    checkout_url = adapter.create_checkout_session(
        customer_id=customer_id,
        price_id=_price_id(),
        success_url=f"{base}/billing?success=1",
        cancel_url=f"{base}/billing",
    )
    return RedirectResponse(checkout_url, status_code=303)


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default=""),
) -> dict:
    """Verify the Stripe signature, parse the event, apply it to local
    state. Returns 200 + ``{"received": True}`` so Stripe doesn't retry."""
    payload = await request.body()
    adapter = get_stripe_adapter()
    try:
        event = adapter.parse_webhook(payload, stripe_signature)
    except Exception as exc:        # noqa: BLE001 — signature validation
        raise HTTPException(status_code=400, detail=f"signature: {exc}") from exc
    apply_stripe_event(event)
    return {"received": True}
