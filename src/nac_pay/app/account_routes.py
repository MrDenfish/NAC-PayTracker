"""Account deletion route — Danger Zone in Settings leads here.

Immediate hard delete: password + typed "DELETE" confirmation gate a
call to ``storage.delete_account``, which purges every DB row and the
user's disk tree. No grace period (yet) — see ``account_delete.py``'s
docstring for the future deactivate-then-purge plan.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required, authenticate, clear_session
from nac_pay.storage import DEFAULT_USER_ID, UserStore, delete_account

from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()


def _user_id_for(request: Request) -> str:
    """Same resolution pattern as document_routes.py — no Depends() since
    main.py's own routes use the plain session lookup too."""
    if not auth_required():
        return DEFAULT_USER_ID
    return request.session.get("user_id") or DEFAULT_USER_ID


def _error(request: Request, message: str) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "account_delete.html",
        {"error": message, "active_screen": "settings"},
        status_code=200,
    )


@router.get("/account/delete", response_class=HTMLResponse)
def account_delete_get(request: Request, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "account_delete.html",
        {"error": error, "active_screen": "settings"},
    )


@router.post("/account/delete", response_class=HTMLResponse)
def account_delete_post(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
) -> HTMLResponse:
    user_id = _user_id_for(request)
    if user_id == DEFAULT_USER_ID:
        return _error(request, "The demo account cannot be deleted.")
    user = UserStore().get(user_id)
    if user is None:
        return _error(request, "Account not found.")
    if confirm.strip() != "DELETE":
        return _error(request, 'Type DELETE (all capitals) to confirm.')
    if authenticate(user.email, password) is None:
        return _error(request, "That password is not correct.")

    delete_account(user_id)
    from .services import invalidate_caches
    invalidate_caches()
    clear_session(request)
    return _TEMPLATES.TemplateResponse(
        request, "account_deleted.html", {"active_screen": ""},
    )
