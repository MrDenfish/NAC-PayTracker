"""FastAPI dependencies + middleware-glue helpers.

``current_user`` is the single seam every route reaches through to know
who's signed in. When ``AUTH_REQUIRED=false`` (dev/tests), it returns the
bundled default user — preserves all pre-auth behavior unchanged. When
true (prod), it reads ``user_id`` from the request session.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from nac_pay.storage import User, UserStore, default_user, get_data_dir


def auth_required() -> bool:
    """Truthy values: 1, true, yes, on (case-insensitive)."""
    raw = os.environ.get("AUTH_REQUIRED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def session_secret() -> str:
    """Required when ``AUTH_REQUIRED=true``. In dev, a stable dev-only
    secret keeps cookies usable across restarts without surfacing it as
    a security artifact."""
    secret = os.environ.get("SESSION_SECRET")
    if secret:
        return secret
    if auth_required():
        raise RuntimeError(
            "SESSION_SECRET env var is required when AUTH_REQUIRED=true"
        )
    return "nac-pay-dev-only-not-secret"


def current_user(request: Request) -> User:
    """Route dependency. Returns the User for this request, or raises 401.

    The AuthRequiredMiddleware handles the redirect-to-login behavior for
    unauthenticated browsers; this dependency is the data-access seam.
    """
    if not auth_required():
        return default_user()
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        raise HTTPException(status_code=401)
    user = UserStore(get_data_dir()).get(user_id)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def set_session_user(request: Request, user_id: str) -> None:
    request.session["user_id"] = user_id


def clear_session(request: Request) -> None:
    request.session.clear()
