"""Auth route handlers — signup, login, verify, forgot, reset, logout.

Mounted under ``/`` directly (not a prefix) so URLs match the spec
(``/login`` etc.). Templates live in ``templates/`` alongside the
existing screens.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import (
    authenticate,
    clear_session,
    consume_email_verification,
    consume_password_reset,
    create_user,
    email_exists,
    find_by_email,
    is_email_verified,
    issue_email_verification,
    issue_password_reset,
    mark_email_verified,
    send_password_reset_email,
    send_verification_email,
    set_session_user,
    update_password,
)
from nac_pay.billing import start_trial

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


def _validate_password(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    return None


# ── Signup ────────────────────────────────────────────────────────────


@router.get("/signup", response_class=HTMLResponse)
def signup_get(request: Request, sent: int = 0, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/signup.html",
        {"sent": bool(sent), "error": error, "active_screen": "auth"},
    )


@router.post("/signup")
def signup_post(
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
) -> RedirectResponse:
    email = email.strip().lower()
    if password != confirm:
        return RedirectResponse(
            "/signup?error=Passwords+do+not+match.",
            status_code=303,
        )
    err = _validate_password(password)
    if err:
        return RedirectResponse(f"/signup?error={err.replace(' ', '+')}", status_code=303)
    if "@" not in email or "." not in email:
        return RedirectResponse("/signup?error=Enter+a+valid+email.", status_code=303)
    if email_exists(email):
        return RedirectResponse(
            "/signup?error=That+email+already+has+an+account.+Try+%2Flogin.",
            status_code=303,
        )
    user_id = create_user(email, password)
    token = issue_email_verification(user_id)
    send_verification_email(email, token)
    return RedirectResponse("/signup?sent=1", status_code=303)


# ── Email verification ───────────────────────────────────────────────


@router.get("/verify/{token}", response_class=HTMLResponse)
def verify_get(request: Request, token: str) -> HTMLResponse:
    lookup = consume_email_verification(token)
    if not lookup.valid:
        return _TEMPLATES.TemplateResponse(
            request,
            "auth/verify_failed.html",
            {"reason": lookup.reason, "active_screen": "auth"},
            status_code=410,
        )
    mark_email_verified(lookup.user_id)
    start_trial(lookup.user_id)        # 90-day no-card trial begins now
    set_session_user(request, lookup.user_id)
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/verify_success.html",
        {"active_screen": "auth"},
    )


# ── Login ────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_get(
    request: Request, error: str = "", reset: int = 0
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/login.html",
        {"error": error, "reset": bool(reset), "active_screen": "auth"},
    )


@router.post("/login")
def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    user_id = authenticate(email, password)
    if user_id is None:
        return RedirectResponse(
            "/login?error=Email+or+password+is+wrong.",
            status_code=303,
        )
    if not is_email_verified(user_id):
        return RedirectResponse(
            "/login?error=Please+verify+your+email+first.",
            status_code=303,
        )
    set_session_user(request, user_id)
    return RedirectResponse("/", status_code=303)


# ── Logout ───────────────────────────────────────────────────────────


@router.post("/logout")
def logout_post(request: Request) -> RedirectResponse:
    clear_session(request)
    return RedirectResponse("/login", status_code=303)


# ── Forgot ───────────────────────────────────────────────────────────


@router.get("/forgot", response_class=HTMLResponse)
def forgot_get(request: Request, sent: int = 0) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/forgot.html",
        {"sent": bool(sent), "active_screen": "auth"},
    )


@router.post("/forgot")
def forgot_post(email: str = Form(...)) -> RedirectResponse:
    user_id = find_by_email(email)
    if user_id is not None:
        token = issue_password_reset(user_id)
        send_password_reset_email(email.strip().lower(), token)
    # Always show the same screen — don't leak whether the email exists.
    return RedirectResponse("/forgot?sent=1", status_code=303)


# ── Reset ────────────────────────────────────────────────────────────


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_get(request: Request, token: str, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "auth/reset.html",
        {"token": token, "error": error, "active_screen": "auth"},
    )


@router.post("/reset/{token}")
def reset_post(
    token: str,
    password: str = Form(...),
    confirm: str = Form(...),
) -> RedirectResponse:
    if password != confirm:
        return RedirectResponse(
            f"/reset/{token}?error=Passwords+do+not+match.",
            status_code=303,
        )
    err = _validate_password(password)
    if err:
        return RedirectResponse(
            f"/reset/{token}?error={err.replace(' ', '+')}",
            status_code=303,
        )
    lookup = consume_password_reset(token)
    if not lookup.valid:
        raise HTTPException(
            status_code=410,
            detail=f"Reset link {lookup.reason}",
        )
    update_password(lookup.user_id, password)
    return RedirectResponse("/login?reset=1", status_code=303)
