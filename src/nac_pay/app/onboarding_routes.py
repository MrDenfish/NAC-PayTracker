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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from .services import (
    invalidate_caches,
    load_persisted_profile,
    normalize_name,
    shared_pilot_directory,
)
from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()


def _user_id_for(request: Request) -> str | None:
    if not auth_required():
        return DEFAULT_USER_ID
    return request.session.get("user_id")


def _directory_lastname(entries, code: str, position: str) -> str:
    """Last name for a (code, seat) in the shared directory. Prefer the exact
    seat; fall back to the code alone (single-seat, or seat not yet chosen) so
    a returning pilot's confirmation still renders."""
    if not code:
        return ""
    exact = next(
        (e for e in entries if e.code == code and e.position == position), None
    )
    if exact is not None:
        return exact.last_name
    any_seat = next((e for e in entries if e.code == code), None)
    return any_seat.last_name if any_seat is not None else ""


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
    user_id = _user_id_for(request)
    persisted = load_persisted_profile(user_id)
    # Fresh (no saved profile row) users must see empty fields — falling
    # back to the bundled DEFAULT_PERSISTED example profile's values here
    # is exactly the "Fred Smith inherited DFI / 124.59" bug this fixes.
    fresh = not PilotProfileStore(get_data_dir(), user_id).exists()
    month_label, entries = shared_pilot_directory()
    pilot_id_value = "" if fresh else persisted.profile.pilot_id
    saved_pos = "" if fresh else getattr(persisted.profile.position, "value", "")
    # A saved code the CURRENT shared FA no longer recognizes (a stale
    # signup, or an admin republish that dropped the pilot) would otherwise
    # render the last-name search permanently disabled with no way to
    # recover it — drop it so the search re-enables. Only when we actually
    # have a directory to check against; an unpublished FA can't disprove
    # anything, so a persisted code still shows in that case.
    codes = {e.code for e in entries}
    if entries and pilot_id_value and pilot_id_value not in codes:
        pilot_id_value = ""
    pilot_id_lastname = _directory_lastname(entries, pilot_id_value, saved_pos)
    return _TEMPLATES.TemplateResponse(
        request,
        "onboarding/profile.html",
        {
            "step": 1,
            "step_total": 3,
            "error": error,
            "persisted": persisted,
            "fresh": fresh,
            "month_label": month_label,
            # Single source of truth for the code widget's state — the
            # template must use THIS, not recompute from persisted/form
            # itself, or a route-side correction (like dropping a stale
            # code below) never reaches the page. POST's _rerender feeds
            # the same key for the same reason.
            "pilot_id_value": pilot_id_value,
            "pilot_id_lastname": pilot_id_lastname,
            "active_screen": "onboarding",
            "form": None,
        },
    )


# ── Pilot-code assist ────────────────────────────────────────────────


@router.get("/onboarding/code-lookup")
def onboarding_code_lookup(
    code: str = "", last_name: str = "",
) -> JSONResponse:
    """Live check against the shared Final Award — exact code match, or a
    case-insensitive last-name prefix search (max 10). Backs the "Find my
    code" helper and the inline check on step 1."""
    label, entries = shared_pilot_directory()
    matches: list[dict[str, str]] = []
    if code.strip():
        c = code.strip().upper()
        # A code shared across seats returns BOTH pilots — the UI shows each
        # tagged by seat so the pilot picks the right one.
        matches = [
            {"code": e.code, "last_name": e.last_name, "position": e.position}
            for e in entries if e.code == c
        ]
    elif last_name.strip():
        q = normalize_name(last_name.strip())
        matches = [
            {"code": e.code, "last_name": e.last_name, "position": e.position}
            for e in entries
            if normalize_name(e.last_name).startswith(q)
        ][:10]
    return JSONResponse({"month_label": label, "matches": matches})


@router.post("/onboarding/profile", response_model=None)
def onboarding_profile_post(
    request: Request,
    # Form("") rather than Form(...): this FastAPI version treats an
    # all-blank required Form field as "missing" (422) rather than an
    # empty string, which would short-circuit before our own "Enter your
    # display name" etc. validation ever ran. Required-ness is enforced
    # by the checks below instead, so every blank-fields submission
    # re-renders with a proper error banner, not a raw 422.
    name: str = Form(""),
    pilot_id: str = Form(""),
    position: str = Form(""),
    hourly_rate: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    user_id = _user_id_for(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=303)

    pilot_id_clean = (pilot_id or "").strip().upper()

    def _rerender(error: str) -> HTMLResponse:
        month_label, entries = shared_pilot_directory()
        # A carried pilot_id the CURRENT directory has just proven invalid
        # (rejected code, malformed shape) must not render as a disabled,
        # dead-end search box — the error banner already explains why, so
        # drop it and re-enable the search instead of trapping the pilot
        # into reloading and losing name/position/rate. A code the
        # directory HASN'T disproven (still a match, or nothing published
        # to check against) stays shown.
        codes = {e.code for e in entries}
        shown_pilot_id = pilot_id_clean
        if entries and pilot_id_clean and pilot_id_clean not in codes:
            shown_pilot_id = ""
        return _TEMPLATES.TemplateResponse(
            request, "onboarding/profile.html",
            {
                "step": 1, "step_total": 3, "error": error,
                "persisted": load_persisted_profile(user_id),
                "fresh": not PilotProfileStore(get_data_dir(), user_id).exists(),
                "month_label": month_label,
                # Same single-source-of-truth key as the GET handler.
                "pilot_id_value": shown_pilot_id,
                "pilot_id_lastname": _directory_lastname(
                    entries, shown_pilot_id, position,
                ),
                "active_screen": "onboarding",
                "form": {
                    "name": name,
                    "position": position, "hourly_rate": hourly_rate,
                },
            },
        )

    # A. All four step-1 fields are required, independent of the client
    # `required` attributes (which a no-JS or scripted submit can bypass).
    if not name.strip():
        return _rerender("Enter your display name")

    try:
        position_enum = Position(position)
    except ValueError:
        return RedirectResponse(
            "/onboarding/profile?error=Pick+FO+or+CPT", status_code=303,
        )
    try:
        rate = Decimal(hourly_rate)
    except InvalidOperation:
        return _rerender("Enter a valid hourly rate")
    if rate <= 0:
        return _rerender("Hourly rate must be positive")

    # B. Find-my-Code is the ONLY path to a pilot_id — hard block. Checked
    # in an order that always names the REAL blocker: an unpublished FA
    # (checked first) used to be masked by the shape-check redirect firing
    # on the inevitably-blank pilot_id, which also discarded the pilot's
    # other preserved fields via a redirect instead of a re-render.
    label, entries = shared_pilot_directory()
    if not entries:
        return _rerender(
            "Signups need the current Final Award published to the site "
            "— it isn't yet. Contact the site admin."
        )
    if not pilot_id_clean:
        return _rerender("Use Find my code to select your pilot code.")
    if len(pilot_id_clean) < 2 or len(pilot_id_clean) > 4:
        return _rerender("Pilot code is 2-4 letters")
    if pilot_id_clean not in {e.code for e in entries}:
        return _rerender(
            f"Code {pilot_id_clean} isn't on the {label} Final Award. "
            "Use Find my code — or contact the site admin if your name "
            "isn't listed."
        )
    # Seat-aware: a code shared across seats (e.g. PBG = a Captain AND a First
    # Officer) must match the position the pilot chose, so the CA pilot can't
    # sign up as the FO pilot with the same code. A seat-less entry ("" — an
    # older sheet whose title lacked a seat) matches any position (the code is
    # then unique anyway, per the resolver's fallback).
    pos_str = position_enum.value
    if not any(
        e.code == pilot_id_clean and e.position in (pos_str, "")
        for e in entries
    ):
        other = next((e for e in entries if e.code == pilot_id_clean), None)
        seat = {"FO": "First Officer", "CPT": "Captain"}
        return _rerender(
            f"Code {pilot_id_clean} is a {seat.get(other.position, other.position)} "
            f"code on the {label} Final Award, but you selected "
            f"{seat.get(pos_str, pos_str)}. Use Find my code to pick the right seat."
        )

    current = load_persisted_profile(user_id)
    new_profile = PilotProfile(
        pilot_id=pilot_id_clean,
        name=name.strip(),
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
