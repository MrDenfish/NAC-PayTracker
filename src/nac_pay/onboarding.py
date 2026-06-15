"""Onboarding state + middleware.

A fresh user — one who has signed up and verified email but hasn't been
through the wizard yet — gets redirected to ``/onboarding`` whenever
they hit a main-app route. Setup paths (``/onboarding/*``, ``/settings``,
``/documents``) stay open so they can complete the flow; auth, billing,
static, and webhook routes are always reachable.

The check is cheap (one column lookup) and short-circuits for the
bundled default dev user, who never needs onboarding.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from nac_pay.auth import auth_required
from nac_pay.storage import DEFAULT_USER_ID
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def should_onboard(user_id: str) -> bool:
    """True iff this user should be routed through the wizard.

    Default user is exempt (their docs are bundled). Any real user
    without ``onboarding_completed_at`` set is fresh."""
    if user_id == DEFAULT_USER_ID:
        return False
    with session_scope() as sess:
        completed = sess.execute(
            select(UserRow.onboarding_completed_at).where(
                UserRow.user_id == user_id
            )
        ).scalar_one_or_none()
        return completed is None


def mark_completed(user_id: str) -> None:
    """Stamp the user as past-onboarded. Idempotent."""
    if user_id == DEFAULT_USER_ID:
        return
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is not None and row.onboarding_completed_at is None:
            row.onboarding_completed_at = _utcnow_iso()


def reset_onboarding(user_id: str) -> None:
    """Test helper / future "redo onboarding" feature."""
    if user_id == DEFAULT_USER_ID:
        return
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is not None:
            row.onboarding_completed_at = None


# Paths that fresh users MUST be able to reach to complete setup.
# Everything else triggers a redirect to /onboarding.
_ONBOARDING_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/settings",
        "/documents",
        "/billing",
        "/login",
        "/signup",
        "/forgot",
        "/logout",
        "/api/health",
    }
)
_ONBOARDING_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/onboarding",
    "/documents/", "/settings/", "/billing/",
    "/verify/", "/reset/",
    "/static/", "/webhooks/",
)


def _is_onboarding_public(path: str) -> bool:
    if path in _ONBOARDING_PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _ONBOARDING_PUBLIC_PREFIXES)


class OnboardingMiddleware(BaseHTTPMiddleware):
    """Redirect fresh users to the wizard. No-op when auth is off."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not auth_required():
            return await call_next(request)
        if _is_onboarding_public(request.url.path):
            return await call_next(request)
        user_id = (
            request.session.get("user_id")
            if hasattr(request, "session")
            else None
        )
        if not user_id:
            return await call_next(request)
        if should_onboard(user_id):
            return RedirectResponse("/onboarding", status_code=303)
        return await call_next(request)
