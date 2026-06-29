"""FastAPI app entry — `uvicorn nac_pay.app.main:app --reload`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .static_version import register as _register_static_v
from starlette.middleware.sessions import SessionMiddleware

from nac_pay.auth import AuthRequiredMiddleware, current_user, session_secret
from nac_pay.auth import auth_required as _auth_required_flag
from nac_pay.billing import SubscriptionRequiredMiddleware
from nac_pay.onboarding import OnboardingMiddleware
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DayOverride,
    PersistedPilotProfile,
    User,
    UserAssignmentVersionStore,
    VersionEntryMode,
    VersionType,
)

from .auth_routes import router as auth_router
from .billing_routes import router as billing_router
from .document_routes import router as document_router
from .onboarding_routes import router as onboarding_router

from .feed_updater import feed_update_loop, updater_enabled
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
_register_static_v(_TEMPLATES)

logger = logging.getLogger("nac_pay.app")


def _configure_app_logging() -> None:
    """Surface the app's own ``nac_pay.*`` INFO logs in the process output.

    Uvicorn configures handlers for its own loggers only — it leaves the
    root logger handler-less, so our named loggers (e.g. the feed updater's
    hourly sweep summary) propagate to nowhere and never appear in
    ``docker logs``. Reuse uvicorn's handler when present (consistent
    formatting), falling back to a plain stream handler otherwise. Idempotent
    — guarded so repeated app startups (e.g. the test client) don't stack
    duplicate handlers."""
    nac_logger = logging.getLogger("nac_pay")
    if nac_logger.handlers:
        return
    uvicorn_logger = logging.getLogger("uvicorn")
    if uvicorn_logger.handlers:
        for handler in uvicorn_logger.handlers:
            nac_logger.addHandler(handler)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        nac_logger.addHandler(handler)
    nac_logger.setLevel(logging.INFO)
    nac_logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the hourly feed updater (when enabled) for the app's lifetime,
    and stop it cleanly on shutdown. Gated by FEED_UPDATER_ENABLED so the
    test suite / local dev don't spawn a network loop."""
    _configure_app_logging()
    stop = asyncio.Event()
    task: asyncio.Task | None = None
    if updater_enabled():
        task = asyncio.create_task(feed_update_loop(stop))
    else:
        logger.info("feed updater disabled (set FEED_UPDATER_ENABLED=true to enable)")
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


app = FastAPI(title="NAC Pay Tracker", version="0.1.0", lifespan=lifespan)

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
    uid = _user_id(request)
    persisted = load_persisted_profile(uid)
    from .feed_updater import last_feed_fetch
    return _TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "persisted": persisted,
            "feed_last_fetched": last_feed_fetch(uid),
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
    # Inline Day-pay editor extras (the "Reason & premium" card omits these).
    pch_value: str = Form(""),
    current_pch: str = Form(""),
    assignment_id: str = Form(""),
) -> RedirectResponse:
    try:
        date.fromisoformat(date_iso)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date {date_iso!r}") from exc

    uid = _user_id(request)
    override = DayOverride(
        date_iso=date_iso,
        reason_code=reason_code or None,
        premium_category=premium_category or None,
        custom_multiplier=custom_multiplier.strip() or None,
        entry_mode=entry_mode or None,
    )
    override_store(uid).save_one(override)

    # Inline Day-pay editor can also adjust PCH. A PCH change is recorded
    # as an append-only REASSIGNMENT version — audited, and subject to the
    # §3.E.1.b greater-of protection (raising takes effect; lowering is
    # protected and uses the "Correct this" flow). Only act when the value
    # actually changed and we have a real account (reassignments are blocked
    # for the default/dev user).
    if pch_value.strip() and uid != DEFAULT_USER_ID:
        try:
            new_pch = Decimal(pch_value.strip())
            cur_pch = Decimal(current_pch.strip()) if current_pch.strip() else None
        except InvalidOperation:
            new_pch = cur_pch = None
        if new_pch is not None and new_pch > 0 and new_pch != cur_pch:
            UserAssignmentVersionStore(user_id=uid).save(
                date_iso=date_iso,
                version_type=VersionType.REASSIGNMENT,
                correction_of=None,
                assignment_id=assignment_id.strip(),
                entry_mode=VersionEntryMode.SIMPLE,
                pch_value=new_pch,
                block_hours=None, duty_hours=None,
                tafb_hours=None, deadhead_pch=None, workdays=None,
                reason_code=reason_code.strip() or "FLOWN",
                premium_category=premium_category.strip() or "NONE",
                notes="PCH adjusted via Day-pay editor",
            )

    invalidate_caches()
    return RedirectResponse(f"/day/{date_iso}?saved=1", status_code=303)


# ── Phase G: reassignment / correction entry ───────────────────────


@app.post("/day/{date_iso}/reassign")
def day_reassign(
    request: Request,
    date_iso: str,
    version_type: str = Form("REASSIGNMENT"),
    correction_of: str = Form(""),
    assignment_id: str = Form(""),
    entry_mode: str = Form("SIMPLE"),
    # Reserve callout: a checkbox on RSV days that flags this reassignment as a
    # "called in during the reserve window" version (drives the ⚡ marker).
    called_in: str = Form(""),
    # Simple mode
    pch_value: str = Form(""),
    # Detailed mode
    block_hours: str = Form(""),
    duty_hours: str = Form(""),
    tafb_hours: str = Form(""),
    deadhead_pch: str = Form("0"),
    workdays: str = Form("1"),
    # Detailed-mode legs (parallel lists; the JS computes block/duty/TAFB from
    # them client-side — here we persist them for the merged "Legs" display).
    leg_flight: list[str] = Form([]),
    leg_out: list[str] = Form([]),
    leg_in: list[str] = Form([]),
    # Labels
    reason_code: str = Form("FLOWN"),
    premium_category: str = Form("NONE"),
    notes: str = Form(""),
) -> RedirectResponse:
    """Append a pilot-recorded version (reassignment or correction).

    Append-only — this route never edits an existing row (removal is a
    separate explicit action, see ``day_version_delete``). A CORRECTION
    must reference an existing seq; that seq is then treated as
    superseded by the engine's active-versions resolver, but the row
    survives in the audit history."""

    def _bail(err: str) -> RedirectResponse:
        from urllib.parse import quote
        return RedirectResponse(
            f"/day/{date_iso}?reassign_error={quote(err)}",
            status_code=303,
        )

    try:
        target_date = date.fromisoformat(date_iso)
    except ValueError:
        raise HTTPException(400, f"Invalid date {date_iso!r}")

    try:
        vt = VersionType(version_type)
    except ValueError:
        return _bail(f"Invalid version_type {version_type!r}")
    try:
        em = VersionEntryMode(entry_mode)
    except ValueError:
        return _bail(f"Invalid entry_mode {entry_mode!r}")

    # "Called in during reserve window" promotes a plain reassignment to a
    # RESERVE_CALLOUT (pay is unchanged; it only marks the day). Never applies
    # to corrections — those carry the type of the row they supersede.
    if vt is VersionType.REASSIGNMENT and called_in.strip():
        vt = VersionType.RESERVE_CALLOUT

    uid = _user_id(request)
    if uid == DEFAULT_USER_ID:
        return _bail("Default user cannot record reassignments — use a real account.")

    # A reserve callout only makes sense on a reserve (RSV) day — you can only
    # be called in off reserve. Validate server-side against the schedule.
    if vt is VersionType.RESERVE_CALLOUT:
        try:
            day_kind = load_day(
                target_date.year, target_date.month, target_date.day, user_id=uid,
            ).kind
        except ValueError:
            day_kind = None
        if day_kind != "reserve":
            return _bail("Reserve callout can only be recorded on a reserve (RSV) day.")

    store = UserAssignmentVersionStore(user_id=uid)

    correction_of_int: int | None = None
    if vt is VersionType.CORRECTION:
        if not correction_of:
            return _bail("Correction needs a version to supersede.")
        try:
            correction_of_int = int(correction_of)
        except ValueError:
            return _bail(f"Invalid correction_of {correction_of!r}")
        # Validate target exists and is itself not a CORRECTION
        # (chain-of-corrections is supported by the resolver, but disallowed
        # at write time — keeps the audit log understandable).
        existing = {v.seq: v for v in store.list_for_date(date_iso)}
        target = existing.get(correction_of_int)
        if target is None:
            return _bail(f"No version seq={correction_of_int} on {date_iso}.")
        if target.version_type is VersionType.CORRECTION:
            return _bail("Can't correct a correction — submit a fresh one against the original.")

    if em is VersionEntryMode.SIMPLE:
        try:
            pch_dec = Decimal(pch_value)
        except InvalidOperation:
            return _bail("Enter a valid PCH value.")
        block_dec = duty_dec = tafb_dec = dh_dec = None
        workdays_int = None
    else:
        try:
            block_dec = Decimal(block_hours)
            duty_dec = Decimal(duty_hours)
            tafb_dec = Decimal(tafb_hours)
            dh_dec = Decimal(deadhead_pch) if deadhead_pch else Decimal("0")
            workdays_int = int(workdays) if workdays else 1
        except (InvalidOperation, ValueError):
            return _bail("Detailed mode needs valid numeric block/duty/TAFB/workdays.")
        if min(block_dec, duty_dec, tafb_dec) < 0 or workdays_int < 1:
            return _bail("Detailed-mode inputs must be non-negative; workdays ≥ 1.")
        from nac_pay.engine import recompute_pch_from_times
        pch_dec = recompute_pch_from_times(
            block_hours=block_dec, duty_hours=duty_dec,
            tafb_hours=tafb_dec, workdays=workdays_int,
            deadhead=dh_dec,
        )

    if pch_dec <= 0:
        return _bail("PCH must be positive.")

    saved = store.save(
        date_iso=date_iso,
        version_type=vt,
        correction_of=correction_of_int,
        assignment_id=assignment_id.strip(),
        entry_mode=em,
        pch_value=pch_dec,
        block_hours=block_dec, duty_hours=duty_dec,
        tafb_hours=tafb_dec, deadhead_pch=dh_dec,
        workdays=workdays_int,
        reason_code=reason_code.strip() or "FLOWN",
        premium_category=premium_category.strip() or "NONE",
        notes=notes.strip()[:500],
    )

    # Persist any complete legs the pilot entered (DETAILED mode), for the
    # merged "Legs" display tagged as Manual. Partial rows are skipped.
    if em is VersionEntryMode.DETAILED:
        from nac_pay.storage import VersionLeg
        legs = [
            VersionLeg(
                flight=(leg_flight[i].strip() if i < len(leg_flight) else ""),
                out_local=out.strip(),
                in_local=leg_in[i].strip(),
            )
            for i, out in enumerate(leg_out)
            if i < len(leg_in) and out.strip() and leg_in[i].strip()
        ]
        if legs:
            store.save_legs(date_iso, saved.seq, legs)

    invalidate_caches()
    return RedirectResponse(
        f"/day/{date_iso}?saved=reassign", status_code=303,
    )


@app.post("/day/{date_iso}/drop")
def day_drop(
    request: Request,
    date_iso: str,
    # Required: drops can only occur with company approval. Server-enforced —
    # the save is rejected unless this checkbox is ticked.
    company_approved: str = Form(""),
    assignment_id: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    """Record a company-approved DROP of a scheduled assignment.

    A drop is the inverse of a reassignment: it forfeits the assignment.
    Stored as a ``VersionType.DROP`` row (pch 0); apply_user_versions then
    stamps the matched Trip/Day with ``ReasonCode.VOLUNTARY_DROP`` so the
    engine credits 0 PCH, drops the workday, and forfeits the floor 1:1 by
    the lost PCH (§3.D). Reverse it from the history's "Restore" link, which
    files a CORRECTION superseding the drop."""

    def _bail(err: str) -> RedirectResponse:
        from urllib.parse import quote
        return RedirectResponse(
            f"/day/{date_iso}?reassign_error={quote(err)}",
            status_code=303,
        )

    try:
        target_date = date.fromisoformat(date_iso)
    except ValueError:
        raise HTTPException(400, f"Invalid date {date_iso!r}")

    uid = _user_id(request)
    if uid == DEFAULT_USER_ID:
        return _bail("Default user cannot record drops — use a real account.")

    if not company_approved.strip():
        return _bail("Company approval is required to drop an assignment.")

    # Only a scheduled, paying assignment can be dropped. An OFF day (or a
    # 0-PCH day) has nothing to forfeit.
    try:
        detail = load_day(
            target_date.year, target_date.month, target_date.day, user_id=uid,
        )
    except ValueError:
        return _bail("No assignment found on this day to drop.")
    if detail.kind == "off" or not detail.effective_pch or detail.effective_pch <= 0:
        return _bail("Nothing to drop — this day has no paying assignment.")

    # Default the dropped-assignment label to whatever the day currently shows.
    aid = assignment_id.strip() or (detail.assignment_id or "")

    note = "Company-approved drop"
    if notes.strip():
        note = f"{note} — {notes.strip()[:400]}"

    UserAssignmentVersionStore(user_id=uid).save(
        date_iso=date_iso,
        version_type=VersionType.DROP,
        correction_of=None,
        assignment_id=aid,
        entry_mode=VersionEntryMode.SIMPLE,
        pch_value=Decimal("0"),
        block_hours=None, duty_hours=None,
        tafb_hours=None, deadhead_pch=None, workdays=None,
        reason_code="VOLUNTARY_DROP",
        premium_category="NONE",
        notes=note,
    )
    invalidate_caches()
    return RedirectResponse(f"/day/{date_iso}?saved=drop", status_code=303)


@app.post("/day/{date_iso}/version/{seq}/delete")
def day_version_delete(
    request: Request, date_iso: str, seq: int,
) -> RedirectResponse:
    """Hard-delete a pilot-recorded assignment version (and cascade to any
    corrections that supersede it). Unlike the append-only save path, this
    removes the row outright — for clearing a typo or a duplicate entry from
    the assignment history. seq 0 (the FA/packet "Original") is not a stored
    row and cannot be deleted."""

    def _bail(err: str) -> RedirectResponse:
        from urllib.parse import quote
        return RedirectResponse(
            f"/day/{date_iso}?reassign_error={quote(err)}", status_code=303,
        )

    try:
        date.fromisoformat(date_iso)
    except ValueError:
        raise HTTPException(400, f"Invalid date {date_iso!r}")

    uid = _user_id(request)
    if uid == DEFAULT_USER_ID:
        return _bail("Default user cannot edit versions — use a real account.")
    if seq < 1:
        return _bail("The original assignment can't be deleted.")

    deleted = UserAssignmentVersionStore(user_id=uid).delete(date_iso, seq)
    if not deleted:
        return _bail("No such version to delete.")
    invalidate_caches()
    return RedirectResponse(
        f"/day/{date_iso}?saved=version_deleted", status_code=303,
    )


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
    saved_q = request.query_params.get("saved", "")
    correct_q = request.query_params.get("correct", "")
    try:
        correct_seq = int(correct_q) if correct_q else None
    except ValueError:
        correct_seq = None
    try:
        data = load_day(
            target.year, target.month, target.day,
            user_id=_user_id(request),
            saved=(saved_q == "1"),
            saved_reassign=(saved_q == "reassign"),
            saved_drop=(saved_q == "drop"),
            saved_version_deleted=(saved_q == "version_deleted"),
            reassign_error=request.query_params.get("reassign_error", ""),
            correct_seq=correct_seq,
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
