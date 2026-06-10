"""Data loader: full pipeline → view records ready for templating.

Pipeline:
  Final Award PDF
    → parse_master_schedule
    → month_from_master_schedule (baseline Month)
  Trip Pairing Packet PDF
    → parse_trip_pairing_packet
    → validate_trip_pairing_packet (§9 discrepancies)
  iCal feed (optional)
    → parse_ical_feed
    → reconcile_feed_to_packet
  apply_actuals_to_month → updated Month
  lower_month → engine input
  compute_pay → EngineResult

The expensive part — parsing PDFs / iCal, running reconciliation, applying
events — happens in ``_pipeline``. It returns a ``PipelineResult`` cached
per (year, month, pilot_code). Each screen-specific loader
(``load_dashboard``, ``load_calendar``, ...) is a thin projection over
that shared result.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date as date_t
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from nac_pay.engine import EngineResult, WinningOption, compute_pay
from nac_pay.parsers import (
    ParsedFeed,
    ValidationDiscrepancy,
    parse_ical_feed,
    parse_master_schedule,
    parse_trip_pairing_packet,
    reconcile_feed_to_packet,
    validate_trip_pairing_packet,
)
from nac_pay.schedule import (
    AppliedEvent,
    Day,
    DutyType,
    Month,
    PilotProfile,
    Position,
    Trip,
    apply_actuals_to_month,
    lower_month,
    month_from_master_schedule,
)

DEFAULT_PILOT = PilotProfile(
    pilot_id="DFI",
    name="Dennis FISHER",
    position=Position.FO,
    hourly_rate=Decimal("124.59"),
)

DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"

# (year, month) → (final award path, packet path, ical path or None)
_DOC_INDEX: dict[tuple[int, int], tuple[Path, Path, Path | None]] = {
    (2026, 5): (
        DOCS_ROOT / "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf",
        DOCS_ROOT / "MAY  2026  Trip Pairing Packet.pdf",
        None,
    ),
    (2026, 6): (
        DOCS_ROOT / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf",
        DOCS_ROOT / "JUNE 2026 Trip Pairing Packet.pdf",
        DOCS_ROOT / "iCal_schedule_feed.ics",
    ),
}

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ── Shared pipeline result ─────────────────────────────────────────────


@dataclass(frozen=True)
class PipelineResult:
    pilot: PilotProfile
    year: int
    month: int
    updated_month: Month
    engine_result: EngineResult
    applied_events: tuple[AppliedEvent, ...]
    validation_discrepancies: tuple[ValidationDiscrepancy, ...]
    feed: ParsedFeed | None
    packet_trip_count: int
    fa_loaded: bool
    packet_loaded: bool


def available_months() -> tuple[tuple[int, int, str], ...]:
    """Months with bundled data, newest first."""
    return tuple(
        sorted(
            ((y, m, f"{_MONTH_NAMES[m]} {y}") for (y, m) in _DOC_INDEX),
            key=lambda t: (t[0], t[1]),
            reverse=True,
        )
    )


@lru_cache(maxsize=8)
def _pipeline(
    year: int,
    month: int,
    pilot_code: str = "DFI",
) -> PipelineResult:
    paths = _DOC_INDEX.get((year, month))
    if paths is None:
        raise ValueError(f"No data bundled for {_MONTH_NAMES[month]} {year}")
    fa_path, packet_path, feed_path = paths
    pilot = DEFAULT_PILOT if pilot_code == "DFI" else DEFAULT_PILOT  # multi-pilot TBD

    fa_grids = parse_master_schedule(str(fa_path))
    sched = fa_grids.get(pilot_code)
    if sched is None:
        raise ValueError(
            f"Pilot {pilot_code} not found in {fa_path.name}. "
            f"Available: {sorted(fa_grids)}"
        )
    baseline, _warnings = month_from_master_schedule(sched, pilot)

    packet = parse_trip_pairing_packet(str(packet_path))
    validation = tuple(validate_trip_pairing_packet(packet))

    feed: ParsedFeed | None = None
    reconciliation = None
    if feed_path is not None and feed_path.exists():
        feed = parse_ical_feed(str(feed_path))
        reconciliation = reconcile_feed_to_packet(feed, packet)

    if reconciliation is not None:
        updated, applied = apply_actuals_to_month(baseline, reconciliation)
    else:
        updated, applied = baseline, ()

    engine_result = compute_pay(lower_month(updated))

    return PipelineResult(
        pilot=pilot,
        year=year,
        month=month,
        updated_month=updated,
        engine_result=engine_result,
        applied_events=tuple(applied),
        validation_discrepancies=validation,
        feed=feed,
        packet_trip_count=len(packet),
        fa_loaded=True,
        packet_loaded=True,
    )


# ── Dashboard view ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DashboardData:
    pilot: PilotProfile
    year: int
    month: int
    month_label: str
    available_months: tuple[tuple[int, int, str], ...]

    line_value: Decimal
    base_monthly_pch: Decimal
    winning_option: str
    option1_floor: Decimal
    option2_workdays_dpg: Decimal
    option3_earned: Decimal
    earned_dollars: Decimal
    topup_pch: Decimal
    topup_dollars: Decimal
    total_pay: Decimal

    fa_loaded: bool
    packet_loaded: bool
    feed_loaded: bool
    packet_trip_count: int
    feed_event_count: int

    applied_events: tuple[AppliedEvent, ...] = ()
    validation_discrepancies: tuple[ValidationDiscrepancy, ...] = ()


def load_dashboard(
    year: int,
    month: int,
    pilot_code: str = "DFI",
) -> DashboardData:
    pr = _pipeline(year, month, pilot_code)
    r = pr.engine_result
    feed = pr.feed
    return DashboardData(
        pilot=pr.pilot,
        year=pr.year,
        month=pr.month,
        month_label=f"{_MONTH_NAMES[pr.month]} {pr.year}",
        available_months=available_months(),
        line_value=pr.updated_month.line_value,
        base_monthly_pch=r.base_monthly_pch,
        winning_option=_winning_option_label(r.winning_option),
        option1_floor=r.option1_floor,
        option2_workdays_dpg=r.option2_workdays_dpg,
        option3_earned=r.option3_earned,
        earned_dollars=r.earned_dollars,
        topup_pch=r.topup_pch,
        topup_dollars=r.topup_dollars,
        total_pay=r.total_pay,
        fa_loaded=pr.fa_loaded,
        packet_loaded=pr.packet_loaded,
        feed_loaded=feed is not None,
        packet_trip_count=pr.packet_trip_count,
        feed_event_count=feed.total_events if feed else 0,
        applied_events=pr.applied_events,
        validation_discrepancies=pr.validation_discrepancies,
    )


def _winning_option_label(opt: WinningOption) -> str:
    return {
        WinningOption.FLOOR: "Guarantee floor",
        WinningOption.WORKDAYS_DPG: "Workdays × DPG",
        WinningOption.EARNED: "Sum earned",
    }[opt]


# ── Calendar view ─────────────────────────────────────────────────────


# Mapping DutyType → (CSS class suffix, short display label).
_DUTY_DISPLAY: dict[DutyType, tuple[str, str]] = {
    DutyType.FLT: ("flt", "FLT"),
    DutyType.RSV: ("rsv", "RSV"),
    DutyType.PTO: ("pto", "PTO"),
    DutyType.FMLA: ("fmla", "FMLA"),
    DutyType.CLASS: ("training", "CLASS"),
    DutyType.SIM: ("training", "SIM"),
    DutyType.DH: ("dh", "DH"),
    DutyType.VX: ("vx", "VX"),
    DutyType.OFF: ("off", "OFF"),
    DutyType.MOVING: ("moving", "MOVING"),
    DutyType.TAXI: ("taxi", "TAXI"),
    DutyType.HOME_STUDY: ("training", "HS"),
}


@dataclass(frozen=True)
class CalendarCell:
    date: date_t
    in_month: bool
    is_weekend: bool
    assignment_id: str | None
    duty_label: str | None       # short label for cell ("FLT", "RSV", ...)
    duty_class: str | None       # CSS class suffix ("flt", "rsv", ...)
    pch: Decimal | None
    has_callout: bool
    is_reassigned: bool


@dataclass(frozen=True)
class CalendarLegendEntry:
    duty_class: str
    label: str


@dataclass(frozen=True)
class CalendarData:
    pilot: PilotProfile
    year: int
    month: int
    month_label: str
    available_months: tuple[tuple[int, int, str], ...]
    weekday_headers: tuple[str, ...]              # "Mon", "Tue", ...
    weeks: tuple[tuple[CalendarCell, ...], ...]
    legend: tuple[CalendarLegendEntry, ...]
    total_pch: Decimal
    line_value: Decimal
    monthly_pch: Decimal
    delta_vs_mpg: Decimal


_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def load_calendar(
    year: int,
    month: int,
    pilot_code: str = "DFI",
) -> CalendarData:
    pr = _pipeline(year, month, pilot_code)
    updated = pr.updated_month

    # Index trips and days by date for O(1) cell lookup.
    trip_by_date: dict[date_t, Trip] = {}
    for trip in updated.trips:
        for d in trip.dates:
            trip_by_date.setdefault(d, trip)
    day_by_date: dict[date_t, Day] = {
        d.date: d for d in updated.days if d.date is not None
    }

    cal = _cal.Calendar(firstweekday=_cal.MONDAY)
    weeks: list[tuple[CalendarCell, ...]] = []
    for week_dates in cal.monthdatescalendar(year, month):
        cells = tuple(
            _build_cell(d, month, trip_by_date, day_by_date)
            for d in week_dates
        )
        weeks.append(cells)

    # Legend: distinct duty classes seen across cells, plus the FLT default.
    seen_classes: set[str] = set()
    legend_entries: list[CalendarLegendEntry] = []
    for week in weeks:
        for cell in week:
            if cell.in_month and cell.duty_class and cell.duty_class not in seen_classes:
                seen_classes.add(cell.duty_class)
                legend_entries.append(
                    CalendarLegendEntry(duty_class=cell.duty_class, label=cell.duty_label or "")
                )
    legend_entries.sort(key=lambda e: e.label)

    return CalendarData(
        pilot=pr.pilot,
        year=pr.year,
        month=pr.month,
        month_label=f"{_MONTH_NAMES[pr.month]} {pr.year}",
        available_months=available_months(),
        weekday_headers=_WEEKDAY_LABELS,
        weeks=tuple(weeks),
        legend=tuple(legend_entries),
        total_pch=pr.engine_result.base_monthly_pch,
        line_value=updated.line_value,
        monthly_pch=pr.engine_result.base_monthly_pch,
        delta_vs_mpg=pr.engine_result.base_monthly_pch - Decimal("65"),
    )


def _build_cell(
    d: date_t,
    month: int,
    trip_by_date: dict[date_t, Trip],
    day_by_date: dict[date_t, Day],
) -> CalendarCell:
    in_month = d.month == month
    is_weekend = d.weekday() >= 5

    trip = trip_by_date.get(d)
    day = day_by_date.get(d)

    if trip is not None:
        return CalendarCell(
            date=d,
            in_month=in_month,
            is_weekend=is_weekend,
            assignment_id=trip.trip_id,
            duty_label="FLT",
            duty_class="flt",
            pch=trip.effective_pch,
            has_callout=False,
            is_reassigned=len(trip.versions) > 0,
        )

    if day is not None:
        class_suffix, label = _DUTY_DISPLAY.get(
            day.duty_type, ("other", day.duty_type.value)
        )
        is_callout = day.callout_trip_pch is not None
        # A callout day visually flips to FLT-style (since they flew) but
        # keeps the reserve aid for context.
        display_class = "flt" if is_callout else class_suffix
        display_label = "CALLOUT" if is_callout else label
        from nac_pay.engine.constants import DPG
        pch_display = (
            max(DPG, day.callout_trip_pch) if is_callout else day.pch_value
        )
        return CalendarCell(
            date=d,
            in_month=in_month,
            is_weekend=is_weekend,
            assignment_id=day.label or None,
            duty_label=display_label,
            duty_class=display_class,
            pch=pch_display,
            has_callout=is_callout,
            is_reassigned=False,
        )

    # Off day (no scheduled activity)
    return CalendarCell(
        date=d,
        in_month=in_month,
        is_weekend=is_weekend,
        assignment_id=None,
        duty_label="OFF" if in_month else None,
        duty_class="off" if in_month else "void",
        pch=None,
        has_callout=False,
        is_reassigned=False,
    )
