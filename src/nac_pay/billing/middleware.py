"""SubscriptionRequiredMiddleware — gates the app behind an active trial
or paid subscription.

Layered on top of ``AuthRequiredMiddleware``. By the time this middleware
runs there's already a user_id in the session; we resolve the user's
subscription snapshot and either let the request through or redirect to
``/billing``.

Public paths and the auth flow stay open so a user whose trial has
expired can still reach the billing page. Webhook routes stay open so
Stripe can post events even when the user has no active subscription.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from nac_pay.auth import auth_required

from .state import has_access, snapshot

_BILLING_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/billing",
        "/login",
        "/signup",
        "/forgot",
        "/logout",
        "/api/health",
    }
)
_BILLING_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/verify/", "/reset/", "/static/", "/webhooks/", "/billing/",
)


def _is_billing_public(path: str) -> bool:
    if path in _BILLING_PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _BILLING_PUBLIC_PREFIXES)


class SubscriptionRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not auth_required():
            return await call_next(request)
        if _is_billing_public(request.url.path):
            return await call_next(request)
        user_id = request.session.get("user_id") if hasattr(request, "session") else None
        if not user_id:
            # AuthRequiredMiddleware will redirect to /login; nothing to do here.
            return await call_next(request)
        snap = snapshot(user_id)
        if not has_access(snap):
            return RedirectResponse("/billing", status_code=303)
        return await call_next(request)
