"""Onboarding wizard — three steps to get a fresh user from signed-up to
ready-to-track-pay.

Step 1: Profile (name, pilot 3-letter code, position, hourly rate).
Step 2: Feed link (paste the BlueOne iCal feed URL; fetched immediately).
Step 3: Done (mark completed, redirect to dashboard).

A "Skip for now" link on each step marks completion without populating
the field — fresh users are never trapped in the wizard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required
from nac_pay.onboarding import mark_completed, should_onboard
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DEFAULT_USER_ID,
    PersistedPilotProfile,
    PilotProfileStore,
    get_data_dir,
)

from .feed_updater import FeedFetchError, update_user_feed
from .services import invalidate_caches, load_persisted_profile
from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()


def _user_id_for(request: Request) -> str | None:
    if not auth_required():
        return DEFAULT_USER_ID
    return request.session.get("user_id")


# ── Step router ──────────────────────────────────────────────────────


@router.get("/onboarding")
def onboarding_landing(request: Request) -> RedirectResponse:
    """Send users to step 1 if fresh, dashboard otherwise."""
    user_id = _user_id_for(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=303)
    if not should_onboard(user_id):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/onboarding/profile", status_code=303)


@router.post("/onboarding/skip")
def onboarding_skip(request: Request) -> RedirectResponse:
    """Skip the wizard. User can still set profile + upload docs via
    the regular Settings + Documents pages."""
    user_id = _user_id_for(request)
    if user_id and user_id != DEFAULT_USER_ID:
        mark_completed(user_id)
        invalidate_caches()
    return RedirectResponse("/", status_code=303)


# ── Step 1: Profile ──────────────────────────────────────────────────


@router.get("/onboarding/profile", response_class=HTMLResponse)
def onboarding_profile_get(request: Request, error: str = "") -> HTMLResponse:
    persisted = load_persisted_profile(_user_id_for(request))
    return _TEMPLATES.TemplateResponse(
        request,
        "onboarding/profile.html",
        {
            "step": 1,
            "step_total": 3,
            "error": error,
            "persisted": persisted,
            "active_screen": "onboarding",
        },
    )


@router.post("/onboarding/profile")
def onboarding_profile_post(
    request: Request,
    name: str = Form(...),
    pilot_id: str = Form(...),
    position: str = Form(...),
    hourly_rate: str = Form(...),
) -> RedirectResponse:
    user_id = _user_id_for(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=303)

    try:
        position_enum = Position(position)
    except ValueError:
        return RedirectResponse(
            "/onboarding/profile?error=Pick+FO+or+CPT", status_code=303,
        )
    try:
        rate = Decimal(hourly_rate)
    except InvalidOperation:
        return RedirectResponse(
            "/onboarding/profile?error=Enter+a+valid+hourly+rate",
            status_code=303,
        )
    if rate <= 0:
        return RedirectResponse(
            "/onboarding/profile?error=Hourly+rate+must+be+positive",
            status_code=303,
        )
    pilot_id_clean = (pilot_id or "").strip().upper()
    if len(pilot_id_clean) < 2 or len(pilot_id_clean) > 4:
        return RedirectResponse(
            "/onboarding/profile?error=Pilot+code+is+2-4+letters",
            status_code=303,
        )

    current = load_persisted_profile(user_id)
    new_profile = PilotProfile(
        pilot_id=pilot_id_clean,
        name=name.strip() or current.profile.name,
        position=position_enum,
        hourly_rate=rate,
        fleet="737",
        sick_bank_days=current.profile.sick_bank_days,
        pto_bank_days=current.profile.pto_bank_days,
    )
    PilotProfileStore(get_data_dir(), user_id).save(
        PersistedPilotProfile(
            profile=new_profile,
            feed_url=current.feed_url,
            feed_auto_update=current.feed_auto_update,
        )
    )
    invalidate_caches()
    return RedirectResponse("/onboarding/feed", status_code=303)


# ── Step 2: Feed link ────────────────────────────────────────────────


@router.get("/onboarding/documents")
def onboarding_documents_redirect() -> RedirectResponse:
    """The old step-2 upload page — replaced by the feed-link step."""
    return RedirectResponse("/onboarding/feed", status_code=303)


@router.get("/onboarding/feed", response_class=HTMLResponse)
def onboarding_feed_get(request: Request, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "onboarding/feed.html",
        {"step": 2, "step_total": 3, "error": error, "active_screen": "onboarding"},
    )


@router.post("/onboarding/feed")
def onboarding_feed_post(
    request: Request, feed_url: str = Form(""),
) -> RedirectResponse:
    user_id = _user_id_for(request)
    if user_id is None or user_id == DEFAULT_USER_ID:
        return RedirectResponse("/onboarding/done", status_code=303)

    url = (feed_url or "").strip()
    if not url:
        return RedirectResponse("/onboarding/done", status_code=303)
    if not url.lower().startswith(("http://", "https://")):
        return RedirectResponse(
            "/onboarding/feed?error=The+feed+address+must+start+with+http",
            status_code=303,
        )

    # Fetch once right now (same path as the hourly updater) so the first
    # dashboard already has live data — and so a bad link fails here, where
    # the pilot can fix it, not silently an hour later.
    try:
        result = update_user_feed(user_id, url, today=date.today())
        if result.months and all(not m.ok for m in result.months):
            raise FeedFetchError(result.months[0].detail)
    except FeedFetchError as exc:
        return RedirectResponse(
            f"/onboarding/feed?error={quote_plus(f'Could not read that link: {exc}')}",
            status_code=303,
        )

    current = load_persisted_profile(user_id)
    PilotProfileStore(get_data_dir(), user_id).save(
        PersistedPilotProfile(
            profile=current.profile, feed_url=url, feed_auto_update=True,
        )
    )
    invalidate_caches()
    return RedirectResponse("/onboarding/done", status_code=303)


# ── Step 3: Done ─────────────────────────────────────────────────────


@router.get("/onboarding/done", response_class=HTMLResponse)
def onboarding_done_get(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "onboarding/done.html",
        {
            "step": 3,
            "step_total": 3,
            "active_screen": "onboarding",
        },
    )


@router.post("/onboarding/done")
def onboarding_done_post(request: Request) -> RedirectResponse:
    """Final confirmation — stamps onboarding_completed_at and lands on /."""
    user_id = _user_id_for(request)
    if user_id and user_id != DEFAULT_USER_ID:
        mark_completed(user_id)
        invalidate_caches()
    return RedirectResponse("/", status_code=303)
