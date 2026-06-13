"""Auth middleware — redirects unauthenticated browsers to /login.

Public paths (login/signup/verify/reset/static/health) are exempted so
the auth flow itself stays reachable. When ``AUTH_REQUIRED=false`` this
middleware is a no-op.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .dependencies import auth_required

_PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/login", "/signup", "/forgot", "/api/health", "/logout"}
)
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/verify/", "/reset/", "/static/", "/webhooks/",
)


def _is_public(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


class AuthRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not auth_required():
            return await call_next(request)
        if _is_public(request.url.path):
            return await call_next(request)
        user_id = request.session.get("user_id") if hasattr(request, "session") else None
        if not user_id:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)
