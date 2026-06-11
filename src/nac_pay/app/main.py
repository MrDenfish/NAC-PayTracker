"""FastAPI app entry — `uvicorn nac_pay.app.main:app --reload`."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .services import (
    available_months,
    load_calendar,
    load_compare,
    load_dashboard,
    load_day,
    load_discrepancies,
    load_pay_breakdown,
)

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))

app = FastAPI(title="NAC Pay Tracker", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _default_year_month() -> tuple[int, int]:
    """Default to the latest month for which we have bundled data."""
    options = available_months()
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

    default_year, default_month = _default_year_month()
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_dashboard(target_year, target_month)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {"data": data, "active_screen": "dashboard"},
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
    default_year, default_month = _default_year_month()
    target_year = year or default_year
    target_month = month or default_month
    try:
        data = load_discrepancies(target_year, target_month)
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
    default_year, default_month = _default_year_month()
    target_year = year or default_year
    target_month = month or default_month
    try:
        data = load_compare(target_year, target_month)
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

    default_year, default_month = _default_year_month()
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_pay_breakdown(target_year, target_month)
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
        data = load_day(target.year, target.month, target.day)
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

    default_year, default_month = _default_year_month()
    target_year = year or default_year
    target_month = month or default_month

    try:
        data = load_calendar(target_year, target_month)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "calendar.html",
        {"data": data, "active_screen": "calendar"},
    )
