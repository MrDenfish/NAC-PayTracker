"""FastAPI app entry — `uvicorn nac_pay.app.main:app --reload`."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from nac_pay.auth import AuthRequiredMiddleware, current_user, session_secret
from nac_pay.auth import auth_required as _auth_required_flag
from nac_pay.billing import SubscriptionRequiredMiddleware
from nac_pay.onboarding import OnboardingMiddleware
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import DEFAULT_USER_ID, DayOverride, PersistedPilotProfile, User

from .auth_routes import router as auth_router
from .billing_routes import router as billing_router
from .document_routes import router as document_router
from .onboarding_routes import router as onboarding_router

from .services import (
    DEFAULT_PERSISTED,
    available_months,
    invalidate_caches,
    load_calendar,
    load_compare,
    load_dashboard,
    load_day,
    load_discrepancies,
    load_pay_breakdown,
    load_persisted_profile,
    override_store,
    profile_store,
)

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

app = FastAPI(title="NAC Pay Tracker", version="0.1.0")

# Starlette middleware order: LAST add_middleware is OUTERMOST and runs
# first on the request path. Desired request-time order:
#   1. SessionMiddleware                  (sets up request.session)
#   2. AuthRequiredMiddleware             (redirect to /login if no session)
#   3. SubscriptionRequiredMiddleware     (redirect to /billing if expired)
#   4. OnboardingMiddleware               (redirect fresh users to /onboarding)
#   5. Route handler
# add_middleware is registered in REVERSE order to achieve this stack.
app.add_middleware(OnboardingMiddleware)
app.add_middleware(SubscriptionRequiredMiddleware)
app.add_middleware(AuthRequiredMiddleware)
app.add_middleware(SessionMiddleware, secret_key=session_secret())

app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(document_router)
app.include_router(onboarding_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _user_id(request: Request) -> str:
    """Resolve the active user id from the session, falling back to the
    default (dev) user when auth is off."""
    if not _auth_required_flag():
        return DEFAULT_USER_ID
    return request.session.get("user_id") or DEFAULT_USER_ID


def _default_year_month(user_id: str = DEFAULT_USER_ID) -> tuple[int, int]:
    """Default to the latest month for which we have data for THIS user.
    Falls back to today when the user has nothing yet."""
    options = available_months(user_id)
    if not options:
        today = date.today()
        return today.year, today.month
    y, m, _ = options[0]
    return y, m


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    ym: str | None = Query(default=None),
) -> HTMLResponse:
    """Dashboard view. Accepts either explicit ?year=&month= or a combined
    ?ym=YYYY-M (what the month switcher submits)."""
    if ym and (year is None or month is None):
        try:
            y_str, m_str = ym.split("-", 1)
            year = int(y_str)
            month = int(m_str)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid ym={ym!r}"
            ) from exc

    uid = _user_id(request)
    default_year, default_month = _default_year_month(uid)
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_dashboard(target_year, target_month, uid)
    except ValueError:
        # No documents bundled or uploaded for this month → render a
        # friendly empty state pointing the user to /documents instead
        # of returning 404 (which dead-ends the UX).
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard_empty.html",
            {
                "year": target_year,
                "month": target_month,
                "active_screen": "dashboard",
            },
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {"data": data, "active_screen": "dashboard"},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request) -> HTMLResponse:
    persisted = load_persisted_profile(_user_id(request))
    return _TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "persisted": persisted,
            "active_screen": "settings",
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/settings")
def settings_save(
    request: Request,
    name: str = Form(...),
    position: str = Form(...),
    hourly_rate: str = Form(...),
    pilot_id: str = Form(""),
    sick_bank_days: int = Form(0),
    pto_bank_days: int = Form(0),
    feed_url: str = Form(""),
    feed_auto_update: str = Form(""),
) -> RedirectResponse:
    uid = _user_id(request)
    try:
        position_enum = Position(position)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid position {position!r}") from exc
    try:
        rate = Decimal(hourly_rate)
    except InvalidOperation as exc:
        raise HTTPException(400, f"Invalid hourly_rate {hourly_rate!r}") from exc
    if rate <= 0:
        raise HTTPException(400, "hourly_rate must be positive")
    if sick_bank_days < 0 or pto_bank_days < 0:
        raise HTTPException(400, "bank days cannot be negative")

    # Pilot 3-letter code: normalize to uppercase; default to existing
    # value when the form submits empty so the bundled docs/ lookup still
    # works for the dev/default user.
    current = load_persisted_profile(uid)
    pilot_id_clean = (pilot_id or "").strip().upper() or current.profile.pilot_id

    new_profile = PilotProfile(
        pilot_id=pilot_id_clean,
        name=name,
        position=position_enum,
        hourly_rate=rate,
        fleet=current.profile.fleet,
        sick_bank_days=sick_bank_days,
        pto_bank_days=pto_bank_days,
    )
    profile_store(uid).save(
        PersistedPilotProfile(
            profile=new_profile,
            feed_url=feed_url.strip(),
            feed_auto_update=feed_auto_update == "on",
        )
    )
    invalidate_caches()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/day/{date_iso}")
def day_save(
    request: Request,
    date_iso: str,
    reason_code: str = Form(""),
    premium_category: str = Form(""),
    entry_mode: str = Form(""),
    custom_multiplier: str = Form(""),
) -> RedirectResponse:
    try:
        date.fromisoformat(date_iso)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date {date_iso!r}") from exc

    override = DayOverride(
        date_iso=date_iso,
        reason_code=reason_code or None,
        premium_category=premium_category or None,
        custom_multiplier=custom_multiplier.strip() or None,
        entry_mode=entry_mode or None,
    )
    override_store(_user_id(request)).save_one(override)
    invalidate_caches()
    return RedirectResponse(f"/day/{date_iso}?saved=1", status_code=303)


@app.get("/discrepancies", response_class=HTMLResponse)
def discrepancies_view(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    ym: str | None = Query(default=None),
) -> HTMLResponse:
    if ym and (year is None or month is None):
        try:
            y_str, m_str = ym.split("-", 1)
            year = int(y_str)
            month = int(m_str)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid ym={ym!r}"
            ) from exc
    uid = _user_id(request)
    default_year, default_month = _default_year_month(uid)
    target_year = year or default_year
    target_month = month or default_month
    try:
        data = load_discrepancies(target_year, target_month, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _TEMPLATES.TemplateResponse(
        request,
        "discrepancies.html",
        {"data": data, "active_screen": "discrepancies"},
    )


@app.get("/compare", response_class=HTMLResponse)
def compare_view(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    ym: str | None = Query(default=None),
) -> HTMLResponse:
    if ym and (year is None or month is None):
        try:
            y_str, m_str = ym.split("-", 1)
            year = int(y_str)
            month = int(m_str)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid ym={ym!r}"
            ) from exc
    uid = _user_id(request)
    default_year, default_month = _default_year_month(uid)
    target_year = year or default_year
    target_month = month or default_month
    try:
        data = load_compare(target_year, target_month, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _TEMPLATES.TemplateResponse(
        request,
        "compare.html",
        {"data": data, "active_screen": "compare"},
    )


@app.get("/pay", response_class=HTMLResponse)
def pay_breakdown(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    ym: str | None = Query(default=None),
) -> HTMLResponse:
    if ym and (year is None or month is None):
        try:
            y_str, m_str = ym.split("-", 1)
            year = int(y_str)
            month = int(m_str)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid ym={ym!r}"
            ) from exc

    uid = _user_id(request)
    default_year, default_month = _default_year_month(uid)
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_pay_breakdown(target_year, target_month, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "pay.html",
        {"data": data, "active_screen": "pay"},
    )


@app.get("/day/{date_iso}", response_class=HTMLResponse)
def day_detail(request: Request, date_iso: str) -> HTMLResponse:
    """Day detail view (read-only first cut; edit form is disabled until the
    persistence layer lands)."""
    try:
        target = date.fromisoformat(date_iso)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid date {date_iso!r}: expected YYYY-MM-DD"
        ) from exc
    try:
        data = load_day(
            target.year, target.month, target.day,
            user_id=_user_id(request),
            saved=(request.query_params.get("saved") == "1"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _TEMPLATES.TemplateResponse(
        request,
        "day.html",
        {"data": data, "active_screen": "calendar"},
    )


@app.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    ym: str | None = Query(default=None),
) -> HTMLResponse:
    if ym and (year is None or month is None):
        try:
            y_str, m_str = ym.split("-", 1)
            year = int(y_str)
            month = int(m_str)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid ym={ym!r}"
            ) from exc

    uid = _user_id(request)
    default_year, default_month = _default_year_month(uid)
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_calendar(target_year, target_month, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "calendar.html",
        {"data": data, "active_screen": "calendar"},
    )
