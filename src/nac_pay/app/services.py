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
per (year, month, user_id). Each screen-specific loader
(``load_dashboard``, ``load_calendar``, ...) is a thin projection over
that shared result.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass, field, replace
from datetime import date as date_t
from datetime import datetime as datetime_t
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from nac_pay.engine import EngineResult, WinningOption, compute_pay
from nac_pay.parsers import (
    FlightLegEvent,
    ParsedFeed,
    PayStub,
    ReconciliationResult,
    TripPairing,
    ValidationDiscrepancy,
    parse_ical_feed,
    parse_master_schedule,
    parse_pay_stub,
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
    apply_overrides_to_month,
    apply_user_versions_to_month,
    lower_month,
    month_from_master_schedule,
)
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DayOverride,
    DayOverrideStore,
    DocumentKind,
    PersistedPilotProfile,
    PilotProfileStore,
    User,
    UserAssignmentVersionStore,
    UserDocumentsStore,
    UserStore,
    active_versions,
    default_user,
    get_data_dir,
)

DEFAULT_PILOT = PilotProfile(
    pilot_id="DFI",
    name="Dennis FISHER",
    position=Position.FO,
    hourly_rate=Decimal("124.59"),
)
DEFAULT_PERSISTED = PersistedPilotProfile(profile=DEFAULT_PILOT)


def current_user() -> User:
    """Placeholder — returns the bundled default user. When auth lands,
    this reads from the request's session / JWT and the routes don't
    need to change."""
    return default_user()


def user_store() -> UserStore:
    return UserStore(get_data_dir())


def profile_store(user_id: str | None = None) -> PilotProfileStore:
    return PilotProfileStore(get_data_dir(), user_id or current_user().user_id)


def override_store(user_id: str | None = None) -> DayOverrideStore:
    return DayOverrideStore(get_data_dir(), user_id or current_user().user_id)


def load_persisted_profile(user_id: str | None = None) -> PersistedPilotProfile:
    return profile_store(user_id).load(DEFAULT_PERSISTED)


def invalidate_caches() -> None:
    """Clear pipeline cache — called after any pilot save so subsequent
    requests re-run with the new profile / overrides. Global for now;
    Phase 2 will add per-user invalidation when the cache key change
    is mechanical (DB-backed stores own this concern differently)."""
    _pipeline.cache_clear()

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

# Default-user bundled pay stubs — dev fallback only. Real users upload
# stubs via /documents, which writes to UserDocumentsStore. See
# ``stubs_for_user`` below for the unified resolver.
_BUNDLED_STUBS: dict[tuple[int, int], tuple[Path, ...]] = {
    (2026, 5): (
        DOCS_ROOT / "pay Stubs" / "May_ Base_payStub.pdf",
        DOCS_ROOT / "pay Stubs" / "May_payStub.pdf",
    ),
}


def stubs_for_user(
    user_id: str, year: int, month: int,
) -> tuple[Path, ...]:
    """Resolve pay-stub PDFs for a (user, month).

    Default user gets the bundled corpus; real users get whatever they've
    uploaded via ``UserDocumentsStore.save_stub``. Order is upload-order
    (slot ascending), so semi-monthly chronological order is preserved
    when stubs are uploaded as they're received.
    """
    if user_id == DEFAULT_USER_ID:
        return _BUNDLED_STUBS.get((year, month), ())
    store = UserDocumentsStore(get_data_dir(), user_id)
    return tuple(rec.path for rec in store.list_stubs(year, month))

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
    reconciliation: ReconciliationResult | None
    packet: dict[str, TripPairing]
    packet_trip_count: int
    fa_loaded: bool
    packet_loaded: bool
    # Phase H — date_iso → count of active (non-superseded) pilot
    # reassignment versions. Drives the calendar badge.
    user_version_counts: dict[str, int] = field(default_factory=dict)


def available_months(user_id: str | None = None) -> tuple[tuple[int, int, str], ...]:
    """Months a user has data for, newest first.

    The default (dev) user gets the bundled ``_DOC_INDEX`` months. A real
    SaaS user gets whatever they've uploaded via UserDocumentsStore.
    Falls back to bundled months when no user_id is supplied.
    """
    uid = user_id or current_user().user_id
    if uid == DEFAULT_USER_ID:
        months = sorted(_DOC_INDEX.keys(), reverse=True)
    else:
        months = UserDocumentsStore(get_data_dir(), uid).available_months()
    return tuple(
        (y, m, f"{_MONTH_NAMES[m]} {y}")
        for (y, m) in months
    )


def documents_for_user(
    user_id: str, year: int, month: int,
) -> tuple[Path, Path, Path | None] | None:
    """Resolve (final_award, packet, ical_or_None) paths for a (user, month).

    Returns None when the user has neither uploaded docs nor a bundled
    entry for that month. The pipeline raises a meaningful error in that
    case so the UI can prompt for uploads.
    """
    if user_id == DEFAULT_USER_ID and (year, month) in _DOC_INDEX:
        return _DOC_INDEX[(year, month)]

    store = UserDocumentsStore(get_data_dir(), user_id)
    fa = store.get(year, month, DocumentKind.FINAL_AWARD)
    packet = store.get(year, month, DocumentKind.TRIP_PACKET)
    ical = store.get(year, month, DocumentKind.ICAL_FEED)
    if fa is None or packet is None:
        return None
    return (fa.path, packet.path, ical.path if ical is not None else None)


# NAC's domicile. The Final Award / Trip Packet label trips by **Alaska
# local date**, but iCal events are stored in UTC (local + 8/9h). A trip
# departing the evening of the last day of a month is already the 1st in
# UTC, so month attribution must convert to local date first — otherwise
# that boundary trip leaks into the next month (see §14.10 caveat).
_DOMICILE_TZ = ZoneInfo("America/Anchorage")


def _local_date(dt: datetime_t) -> date_t:
    """Domicile-local civil date of a UTC timestamp (DST handled by tz)."""
    return dt.astimezone(_DOMICILE_TZ).date()


def _in_month(d: date_t, year: int, month: int) -> bool:
    return d.year == year and d.month == month


def _filter_reconciliation_to_month(
    recon: ReconciliationResult, year: int, month: int,
) -> ReconciliationResult:
    """Keep only reconciled trips that START in the target month.

    The BlueOne feed is one stable roster URL spanning many months, so a
    feed loaded for June also contains July's legs. Reconciliation groups
    legs into trips on the FULL feed first (so a trip straddling the month
    boundary keeps all its legs), then this drops trips that belong to a
    different month — otherwise a next-month trip leaks into this month as a
    phantom open-time pickup, inflating PCH. A trip is attributed to the
    month of its first leg (UTC).
    """
    keep = lambda rt: _in_month(_local_date(rt.first_dt_utc), year, month)  # noqa: E731
    return ReconciliationResult(
        trips=tuple(t for t in recon.trips if keep(t)),
        matched=tuple(t for t in recon.matched if keep(t)),
        unmatched=tuple(t for t in recon.unmatched if keep(t)),
    )


def _filter_feed_to_month(feed: ParsedFeed, year: int, month: int) -> ParsedFeed:
    """Scope a parsed feed's events to the target month (for display: event
    counts, unmatched-leg listings). Each event is kept by its own start
    date; the pay path uses the trip-level filter above."""
    inm = lambda ev: _in_month(_local_date(ev.dt_start_utc), year, month)  # noqa: E731
    return ParsedFeed(
        flight_legs=tuple(e for e in feed.flight_legs if inm(e)),
        reserves=tuple(e for e in feed.reserves if inm(e)),
        off_days=tuple(e for e in feed.off_days if inm(e)),
        unknown=tuple(e for e in feed.unknown if inm(e)),
    )


@lru_cache(maxsize=64)
def _pipeline(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> PipelineResult:
    paths = documents_for_user(user_id, year, month)
    if paths is None:
        raise ValueError(
            f"No documents uploaded for {_MONTH_NAMES[month]} {year}. "
            "Upload your Final Award + Trip Pairing Packet via the Documents page."
        )
    fa_path, packet_path, feed_path = paths

    # Resolved per call so a Settings save (which triggers invalidate_caches())
    # picks up changes on the next request.
    persisted = load_persisted_profile(user_id)
    pilot = persisted.profile
    pilot_code = pilot.pilot_id

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
        # Group + reconcile on the FULL feed (boundary trips need all legs),
        # then scope to this month so next-month trips don't leak in.
        reconciliation = _filter_reconciliation_to_month(
            reconcile_feed_to_packet(feed, packet), year, month,
        )
        feed = _filter_feed_to_month(feed, year, month)

    if reconciliation is not None:
        updated, applied = apply_actuals_to_month(baseline, reconciliation)
    else:
        updated, applied = baseline, ()

    # Phase G: fold pilot-recorded assignment versions onto matching
    # trips (reassignments + corrections). The store keeps the full
    # history; we resolve supersession here and pass only ACTIVE versions
    # to the engine so a corrected typo doesn't inflate effective_pch.
    user_versions = UserAssignmentVersionStore(user_id=user_id).list_for_month(year, month)
    active_by_date: dict[str, list] = {}
    user_version_counts: dict[str, int] = {}
    for date_iso, vs in user_versions.items():
        active, _superseded = active_versions(vs)
        if active:
            active_by_date[date_iso] = active
            user_version_counts[date_iso] = len(active)
    if active_by_date:
        updated = apply_user_versions_to_month(updated, active_by_date)

    # Apply pilot overrides LAST so an explicit pilot edit is the final
    # word: it trumps iCal-derived events AND the premium_category a
    # reassignment version adopts by default (§7 — the pilot always has
    # final say). This is what lets the day-detail "Reason & premium"
    # card relabel a reassigned day, e.g. Overtime → Open Time, without
    # the version re-stamping its own premium afterward.
    overrides = override_store(user_id).load_all()
    updated = apply_overrides_to_month(updated, overrides)

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
        reconciliation=reconciliation,
        packet=packet,
        packet_trip_count=len(packet),
        fa_loaded=True,
        packet_loaded=True,
        user_version_counts=user_version_counts,
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

    # Phase I.2 — Regular vs Premium PCH split for the dashboard tile.
    # "Premium" = chunks paying at any multiplier > 1.0 (Open Time,
    # Overtime, Junior Assignment, Landing, Hostile, NRFO-specialized).
    regular_pch: Decimal = Decimal("0")
    premium_pch: Decimal = Decimal("0")
    premium_dollars: Decimal = Decimal("0")

    fa_loaded: bool = True
    packet_loaded: bool = True
    feed_loaded: bool = False
    packet_trip_count: int = 0
    feed_event_count: int = 0

    applied_events: tuple[AppliedEvent, ...] = ()
    validation_discrepancies: tuple[ValidationDiscrepancy, ...] = ()


def load_dashboard(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> DashboardData:
    pr = _pipeline(year, month, user_id)
    r = pr.engine_result
    feed = pr.feed

    # Phase I.2 — split into Regular vs Premium for the dashboard tile.
    regular_pch = Decimal("0")
    premium_pch = Decimal("0")
    premium_dollars = Decimal("0")
    for c in r.per_chunk:
        if c.multiplier > Decimal("1.0"):
            premium_pch += c.raw_pch
            premium_dollars += c.dollars
        else:
            regular_pch += c.raw_pch

    return DashboardData(
        pilot=pr.pilot,
        year=pr.year,
        month=pr.month,
        month_label=f"{_MONTH_NAMES[pr.month]} {pr.year}",
        available_months=available_months(user_id),
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
        regular_pch=regular_pch,
        premium_pch=premium_pch,
        premium_dollars=premium_dollars,
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
    # Phase H: count of active pilot reassignment versions on this date
    # (0 = no pilot reassignment; drives the ↻N badge + tint).
    user_reassignment_count: int = 0
    # Phase H.1: the winning active user version's assignment_id, shown
    # in bold ABOVE the FA-original label so the calendar tells the
    # reader WHAT the day was reassigned to. None when no active version.
    new_assignment_id: str | None = None
    # Phase I.4: subtle premium label under the new assignment (e.g.,
    # "Open Time", "Overtime"). None when the day has no premium.
    premium_label: str | None = None
    # Phase I.5: per-day dollar value (PCH × base_rate × multiplier),
    # rounded to the nearest whole dollar. Rendered in cell bottom-right.
    pay_dollars: Decimal | None = None
    # Company-approved drop: the scheduled assignment was forfeited (0 PCH).
    # The cell shows a DROPPED tag; the FA-original aid stays visible.
    is_dropped: bool = False


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
    # Phase I.6 — total pay $ for the footer, replacing "Δ vs MPG".
    total_pay: Decimal = Decimal("0")


_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def load_calendar(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> CalendarData:
    pr = _pipeline(year, month, user_id)
    updated = pr.updated_month

    # Index trips and days by date for O(1) cell lookup.
    trip_by_date: dict[date_t, Trip] = {}
    for trip in updated.trips:
        for d in trip.dates:
            trip_by_date.setdefault(d, trip)
    day_by_date: dict[date_t, Day] = {
        d.date: d for d in updated.days if d.date is not None
    }

    # Phase H.1 + I.4: load active user versions to surface the winning
    # assignment_id + premium label per date for the calendar cell.
    from nac_pay.storage import UserAssignmentVersionStore as _UAVS
    from nac_pay.storage import VersionType as _VT
    all_user_versions = _UAVS(user_id=user_id).list_for_month(year, month)
    winning_aid_by_date: dict[str, str] = {}
    premium_label_by_date: dict[str, str] = {}
    # Dates with an active RESERVE_CALLOUT version — drives the ⚡ marker for
    # manually-recorded "called in during reserve window" days (the iCal path
    # lights the same bolt via Day.callout_trip_pch).
    callout_by_date: set[str] = set()
    for date_iso, vs in all_user_versions.items():
        active, _sup = active_versions(vs)
        if not active:
            continue
        if any(v.version_type is _VT.RESERVE_CALLOUT for v in active):
            callout_by_date.add(date_iso)
        # Highest pch wins; on a tie the LATEST amendment wins (seq), so a
        # fresh re-entry of the same value becomes effective over an older one.
        winner = max(active, key=lambda v: (v.pch_value, v.seq))
        eff = trip_by_date.get(date_t.fromisoformat(date_iso)) \
            or day_by_date.get(date_t.fromisoformat(date_iso))
        # A genuine reassignment names a DIFFERENT id than the day's own
        # assignment. A PCH-only quick edit copies the current id (e.g. the
        # reserve line "1021" on a callout day) — that must NOT register as a
        # winning aid, or it overrides the flown-trip callout id (the bug that
        # made a callout day's cell revert from "720/..." back to "1021").
        self_id = (
            getattr(eff, "trip_id", None) or getattr(eff, "label", None)
            if eff is not None else None
        )
        if winner.assignment_id and winner.assignment_id != self_id:
            winning_aid_by_date[date_iso] = winner.assignment_id
        # I.4 (fixed 2026-06-19): label the cell with the EFFECTIVE premium
        # from the post-override month, NOT the raw winning version. A
        # DayOverride applies after reassignment versions (see _pipeline),
        # so reading the version's premium here ignored a relabel done on
        # the day page — the calendar kept showing the stale premium.
        eff_premium = (
            eff.premium_category.value if eff is not None
            else winner.premium_category
        )
        label = _PREMIUM_DISPLAY.get(eff_premium)
        if label is not None:
            premium_label_by_date[date_iso] = label

    base_rate = pr.pilot.hourly_rate

    cal = _cal.Calendar(firstweekday=_cal.MONDAY)
    weeks: list[tuple[CalendarCell, ...]] = []
    for week_dates in cal.monthdatescalendar(year, month):
        cells = tuple(
            _build_cell(d, month, trip_by_date, day_by_date,
                        pr.user_version_counts.get(d.isoformat(), 0),
                        winning_aid_by_date.get(d.isoformat()),
                        premium_label_by_date.get(d.isoformat()),
                        base_rate,
                        d.isoformat() in callout_by_date)
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
        available_months=available_months(user_id),
        weekday_headers=_WEEKDAY_LABELS,
        weeks=tuple(weeks),
        legend=tuple(legend_entries),
        total_pch=pr.engine_result.base_monthly_pch,
        line_value=updated.line_value,
        monthly_pch=pr.engine_result.base_monthly_pch,
        delta_vs_mpg=pr.engine_result.base_monthly_pch - Decimal("65"),
        total_pay=pr.engine_result.total_pay,
    )


# ── Day detail view ──────────────────────────────────────────────────


@dataclass(frozen=True)
class PchComponent:
    label: str
    pch: Decimal
    is_winning: bool          # which component is currently winning the max


@dataclass(frozen=True)
class DayLeg:
    flight_no: str
    origin: str
    destination: str
    tail: str
    dt_start_utc: str         # ISO string for display (UTC)
    dt_end_utc: str
    block_hours: Decimal
    out_local: str = ""       # Anchorage-local "HH:MM" for display
    in_local: str = ""
    source: str = "iCal"      # "iCal" (feed) or "Manual" (pilot-entered)


@dataclass(frozen=True)
class DayVersion:
    """One row of the assignment history (Phase G).

    Display-only — combines the trip's original (always present), any
    iCal-derived versions, and any pilot-entered versions (active or
    superseded) into a single ordered list."""

    seq: int                  # display seq (0 = original published)
    pch_value: Decimal
    label: str
    is_effective: bool        # winning the max-PCH comparison
    source: str = "Original"
    """One of: 'Original', 'iCal reconciliation', 'Pilot reassignment',
    'Pilot correction'."""

    # User-version metadata (populated only when source starts with 'Pilot').
    user_seq: int | None = None
    user_version_type: str | None = None
    correction_of: int | None = None
    is_superseded: bool = False
    superseded_by_user_seq: int | None = None
    notes: str = ""
    created_at: str = ""

    # Phase H — per-version trip structure for the expandable detail view.
    # All optional; the template skips missing data.
    entry_mode: str | None = None        # "SIMPLE" or "DETAILED"
    block_hours: Decimal | None = None
    duty_hours: Decimal | None = None
    tafb_hours: Decimal | None = None
    deadhead_pch: Decimal | None = None
    workdays: int | None = None
    # When the version's assignment_id matches a trip in this month's
    # packet, this carries the packet's structural data so the row can
    # show component PCHs even for SIMPLE-mode entries.
    packet_match: PacketTripOption | None = None
    reason_code: str = ""
    premium_category: str = ""


@dataclass(frozen=True)
class DayPayRow:
    """Phase I.7 — one row of the day-detail per-day pay breakdown card.

    Example: Open Time · 1.5× · 3.82 · $186.88 = $713.90. For unmodified
    trip/reserve days, it's a single row at 1.0×. For premium pickups,
    it's the premium row. Layout mirrors the Pay Breakdown screen but
    scoped to one day's chunks."""

    category: str
    multiplier: Decimal
    pch: Decimal
    rate: Decimal
    base_rate: Decimal
    amount: Decimal


@dataclass(frozen=True)
class PacketTripOption:
    """One row of the trip catalog exposed to the reassignment form.

    Used to populate the assignment_id `<datalist>` so the pilot gets
    autocomplete + auto-fill of PCH value. Off-packet entries (assignment
    IDs not in this list) still work — the input is free-text."""

    trip_id: str
    pch_value: Decimal
    sch_block_hours: Decimal
    duty_hours: Decimal
    tafb_hours: Decimal
    workdays: int


@dataclass(frozen=True)
class ReassignFormDefaults:
    """Pre-filled values when the form is opened via ?correct=<seq>.

    When the pilot clicks "Correct this" on a prior pilot version, the
    form re-renders with that version's values + a hidden field carrying
    the correction_of seq. Saves them from retyping everything."""

    version_type: str = "REASSIGNMENT"
    correction_of: int | None = None
    correcting_seq_label: str = ""    # "v3" — for the form header
    entry_mode: str = "SIMPLE"
    assignment_id: str = ""
    pch_value: str = ""
    block_hours: str = ""
    duty_hours: str = ""
    tafb_hours: str = ""
    deadhead_pch: str = "0"
    workdays: str = "1"
    reason_code: str = "FLOWN"
    premium_category: str = "NONE"
    notes: str = ""
    # True when this correction is undoing a DROP (the form re-renders as a
    # "Restore assignment" action, pre-filled with the original PCH). Saving
    # files a CORRECTION that supersedes the drop, reverting the forfeit.
    is_restore: bool = False
    # Pre-filled legs for the Detailed leg table — the iCal legs (so the pilot
    # only adds what's missing and Block/Duty/TAFB compute from the full set),
    # or the corrected version's own legs. Each: {"flight","out","in"}.
    legs: tuple = ()


@dataclass(frozen=True)
class FormOption:
    """One <option> entry for the Day-detail Reason/Premium dropdowns."""

    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class DayDetailData:
    pilot: PilotProfile
    year: int
    month: int
    day: int
    date_iso: str
    date_label: str           # "Friday, June 12, 2026"
    weekday_label: str        # "Fri"

    # Navigation
    prev_date_iso: str | None
    next_date_iso: str | None
    back_to_calendar_url: str

    # Classification
    kind: str                 # "trip" | "reserve" | "off" | "other"
    duty_label: str
    duty_class: str
    assignment_id: str | None
    in_packet: bool

    # PCH
    published_pch: Decimal | None
    effective_pch: Decimal | None
    pch_uplift: Decimal | None     # max(0, effective - published)

    # Reason + premium
    reason_label: str
    premium_label: str
    premium_multiplier: Decimal | None

    # Packet detail (for FLT days only — when matched)
    packet_trip_id: str | None
    packet_components: tuple[PchComponent, ...]
    sch_block_hours: Decimal | None
    sch_duty_hours: Decimal | None
    sch_tafb_hours: Decimal | None

    # iCal leg detail (for FLT days)
    legs: tuple[DayLeg, ...]
    actual_block_hours: Decimal | None
    block_delta: Decimal | None         # actual - scheduled

    # Assignment history (versions)
    versions: tuple[DayVersion, ...]

    # Reserve callout
    callout_trip_pch: Decimal | None
    callout_excess: Decimal | None      # max(0, callout - DPG)

    # Activity log on this date
    applied_events: tuple[AppliedEvent, ...]

    # Editing — form options + saved-banner flag
    reason_options: tuple[FormOption, ...] = ()
    premium_options: tuple[FormOption, ...] = ()
    entry_mode_options: tuple[FormOption, ...] = ()
    has_override: bool = False
    editable: bool = True
    saved: bool = False
    # Company-approved drop active on this day: the assignment is forfeited
    # (0 PCH credited, floor reduced by the lost PCH). Drives the day-detail
    # "Dropped" note + hides the drop affordance.
    is_dropped: bool = False

    # Phase G — pilot reassignment form
    reassign_form_defaults: ReassignFormDefaults = ReassignFormDefaults()
    saved_reassign: bool = False
    saved_drop: bool = False
    saved_version_deleted: bool = False
    reassign_error: str = ""

    # Phase H — packet trip catalog for the assignment_id datalist
    # (autocomplete + PCH auto-fill via tiny JS).
    packet_trip_options: tuple[PacketTripOption, ...] = ()

    # Phase I.7 — per-day pay breakdown rows (category · multiplier ·
    # PCH · rate = amount). One row per chunk crediting this date.
    day_pay_rows: tuple = ()
    day_pay_total: Decimal | None = None

    # Duty window from iCal actuals + contractual padding (§3.E duty rig).
    # Anchorage-local clock strings; duty_hours is the duration.
    duty_on: str | None = None
    duty_off: str | None = None
    duty_hours: Decimal | None = None
    duty_rig_pch: Decimal | None = None
    # Scheduled duty window from the packet (local "HH:MM") + its rig — the
    # reconstruct-from-packet fallback shown when iCal legs are missing.
    sched_duty_on: str | None = None
    sched_duty_off: str | None = None
    sched_duty_rig_pch: Decimal | None = None
    # The day's effective PCH against its candidate sources (DPG /
    # published-or-callout / flight-op / duty-rig); winner = the credited value.
    pch_candidates: tuple[PchComponent, ...] = ()


_WEEKDAY_FULL = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DPG = Decimal("3.82")


def _clock_span_hours(out_s: str, in_s: str) -> Decimal:
    """Hours from an "HH:MM" out clock to an "HH:MM" in clock (rolls past
    midnight). 0 on unparseable input."""
    def _m(s: str):
        try:
            h, mm = s.split(":")
            return int(h) * 60 + int(mm)
        except Exception:
            return None
    o, i = _m(out_s), _m(in_s)
    if o is None or i is None:
        return Decimal("0")
    span = i - o
    if span < 0:
        span += 1440
    return Decimal(span) / Decimal("60")


def _manual_day_legs(version_legs) -> tuple["DayLeg", ...]:
    """Render pilot-entered VersionLegs as DayLegs (source = Manual) for the
    Legs card; per-leg block is computed from the entered clocks. Sorted by
    departure so the card reads chronologically regardless of entry order."""
    ordered = sorted(version_legs, key=lambda lg: lg.out_local or "")
    return tuple(
        DayLeg(
            flight_no=lg.flight, origin="", destination="", tail="",
            dt_start_utc="", dt_end_utc="",
            block_hours=_clock_span_hours(lg.out_local, lg.in_local),
            out_local=lg.out_local, in_local=lg.in_local, source="Manual",
        )
        for lg in ordered
    )

_REASON_LABELS = {
    "FLOWN": "Flown",
    "PTO": "PTO",
    "SICK": "Sick",
    "JURY": "Jury",
    "BEREAVEMENT": "Bereavement",
    "TRAINING": "Training",
    "MOVING": "Moving",
    "FAR": "FAR",
    "MILITARY": "Military",
    "FMLA": "FMLA",
    "UNPAID_LOA": "Unpaid LOA",
    "VOLUNTARY_DROP": "Voluntary drop",
    "LESSER_TRADE": "Lesser trade",
    "UNPROTECTED_UNAVAIL": "Unprotected unavail.",
    "OFF": "Day off",
}

_PREMIUM_LABELS = {
    "NONE": "None (1.0×)",
    "OPEN_TIME_MID_MONTH": "Open time, mid-month (1.5×)",
    "OPEN_TIME_BID_PERIOD": "Open time, bid period (1.0×)",
    "OVERTIME": "Overtime (1.5×)",
    "JUNIOR_ASSIGNMENT_1ST": "Junior assignment 1st (2.0×)",
    "JUNIOR_ASSIGNMENT_NTH": "Junior assignment Nth (2.5×)",
    "LANDING": "Landing (1.5×, leg)",
    "HOSTILE": "Hostile area (2.0×, duty period)",
    "NRFO_SPECIALIZED": "NRFO specialized (1.5×)",
    "CUSTOM": "Custom",
}


def _reason_options(selected_value: str, kind: str) -> tuple[FormOption, ...]:
    from nac_pay.schedule.labels import ReasonCode
    # Only show codes that make sense for the kind. Off-only categories
    # are filtered out when the day has work.
    relevant = (
        ReasonCode.FLOWN,
        ReasonCode.PTO,
        ReasonCode.SICK,
        ReasonCode.JURY,
        ReasonCode.BEREAVEMENT,
        ReasonCode.TRAINING,
        ReasonCode.MOVING,
        ReasonCode.FAR,
        ReasonCode.MILITARY,
        ReasonCode.FMLA,
        ReasonCode.UNPAID_LOA,
        ReasonCode.VOLUNTARY_DROP,
        ReasonCode.LESSER_TRADE,
        ReasonCode.UNPROTECTED_UNAVAIL,
        ReasonCode.OFF,
    )
    return tuple(
        FormOption(value=r.value, label=_REASON_LABELS[r.value],
                   selected=(r.value == selected_value))
        for r in relevant
    )


def _premium_options(selected_value: str) -> tuple[FormOption, ...]:
    from nac_pay.schedule.labels import PremiumCategory
    return tuple(
        FormOption(value=p.value, label=_PREMIUM_LABELS[p.value],
                   selected=(p.value == selected_value))
        for p in PremiumCategory
    )


def _entry_mode_options(selected_value: str) -> tuple[FormOption, ...]:
    from nac_pay.schedule.labels import EntryMode
    labels = {
        EntryMode.SIMPLE.value: "Value (from FA)",
        EntryMode.DETAILED.value: "Detailed (compute from actual times)",
    }
    return tuple(
        FormOption(value=m.value, label=labels[m.value],
                   selected=(m.value == selected_value))
        for m in EntryMode
    )


def load_day(
    year: int,
    month: int,
    day: int,
    user_id: str = DEFAULT_USER_ID,
    *,
    saved: bool = False,
    saved_reassign: bool = False,
    saved_drop: bool = False,
    saved_version_deleted: bool = False,
    reassign_error: str = "",
    correct_seq: int | None = None,
) -> DayDetailData:
    try:
        target = date_t(year, month, day)
    except ValueError as exc:
        raise ValueError(
            f"Invalid date {year}-{month}-{day}: {exc}"
        ) from exc

    pr = _pipeline(year, month, user_id)
    updated = pr.updated_month

    # Find the Trip or Day for this date.
    trip = next(
        (t for t in updated.trips if target in t.dates),
        None,
    )
    day_entry = next(
        (d for d in updated.days if d.date == target),
        None,
    )

    # Phase I.7 — build per-day pay rows from the engine's chunks.
    day_pay_rows, day_pay_total = _build_day_pay_rows(
        pr=pr, trip=trip, day_entry=day_entry,
    )

    # iCal legs on this date.
    legs: tuple[DayLeg, ...] = ()
    actual_block: Decimal | None = None
    duty_on: str | None = None
    duty_off: str | None = None
    duty_hours: Decimal | None = None
    duty_rig_pch: Decimal | None = None
    if pr.feed is not None:
        date_legs = sorted(
            (leg for leg in pr.feed.flight_legs if leg.dt_start_utc.date() == target),
            key=lambda leg: leg.dt_start_utc,
        )
        legs = tuple(
            DayLeg(
                flight_no=leg.flight_no_short,
                origin=leg.origin,
                destination=leg.destination,
                tail=leg.tail,
                dt_start_utc=leg.dt_start_utc.strftime("%Y-%m-%d %H:%MZ"),
                dt_end_utc=leg.dt_end_utc.strftime("%Y-%m-%d %H:%MZ"),
                block_hours=leg.block_hours,
                out_local=leg.dt_start_utc.astimezone(_DOMICILE_TZ).strftime("%H:%M"),
                in_local=leg.dt_end_utc.astimezone(_DOMICILE_TZ).strftime("%H:%M"),
            )
            for leg in date_legs
        )
        if date_legs:
            actual_block = sum(
                (leg.block_hours for leg in date_legs),
                Decimal("0"),
            )
            # Duty window from iCal actuals + contractual padding: report
            # REPORT_PAD before the first leg out, release TRIP_END_PAD after
            # the last leg in. Times shown Anchorage-local; the duration (and
            # duty rig = duty/2) is tz-independent. Auto-computed for display;
            # the pilot can amend the credited value (see the greater-of path).
            from datetime import timedelta as _td

            from nac_pay.engine.constants import (
                REPORT_PAD_HOURS,
                TRIP_END_PAD_HOURS,
            )
            report_pad = _td(hours=float(REPORT_PAD_HOURS))
            end_pad = _td(hours=float(TRIP_END_PAD_HOURS))
            duty_start = date_legs[0].dt_start_utc - report_pad
            duty_end = date_legs[-1].dt_end_utc + end_pad
            duty_on = duty_start.astimezone(_DOMICILE_TZ).strftime("%H:%M")
            duty_off = duty_end.astimezone(_DOMICILE_TZ).strftime("%H:%M")
            duty_hours = (
                Decimal((duty_end - duty_start).total_seconds()) / Decimal("3600")
            )
            duty_rig_pch = duty_hours / Decimal("2")

    # Matched packet trip via reconciliation.
    packet_trip: TripPairing | None = None
    if pr.reconciliation is not None and trip is not None:
        for rt in pr.reconciliation.matched:
            if (
                rt.first_dt_utc.date() == target
                and rt.packet_trip is not None
                and _ordered_subseq(trip.trip_id.split("/"), rt.trip_id.split("/"))
            ):
                packet_trip = rt.packet_trip
                break

    # Applied events on this date.
    events_today = tuple(e for e in pr.applied_events if e.date == target)

    # Check for an existing pilot override on this date.
    override = override_store(user_id).load_all().get(target.isoformat())

    # Phase G: load all user-recorded versions for this date (active +
    # superseded) so the history block can show the full audit trail.
    _av_store = UserAssignmentVersionStore(user_id=user_id)
    user_versions = _av_store.list_for_date(target.isoformat())
    manual_legs_by_seq = _av_store.list_legs_for_date(target.isoformat())
    active, superseded_seqs = active_versions(user_versions)

    # Build the "who supersedes whom" backref map for display.
    superseded_by: dict[int, int] = {}
    for uv in user_versions:
        from nac_pay.storage import VersionType as _VT
        if uv.version_type is _VT.CORRECTION and uv.correction_of is not None:
            superseded_by[uv.correction_of] = uv.seq

    # Pre-fill form when ?correct=<seq> is in the URL. For a drop-restore the
    # original published PCH (the value to revert to) comes from the trip/day.
    if trip is not None:
        restore_pch = trip.published_pch
    elif day_entry is not None:
        restore_pch = (
            day_entry.original_pch
            if day_entry.original_pch is not None
            else day_entry.pch_value
        )
    else:
        restore_pch = None
    reassign_defaults = _build_reassign_defaults(
        user_versions, correct_seq, restore_pch,
        ical_legs=legs, manual_legs_by_seq=manual_legs_by_seq,
    )

    # Packet trip catalog for the assignment_id <datalist>. Sorted by id
    # so the dropdown is scannable.
    packet_options = tuple(
        PacketTripOption(
            trip_id=tp.trip_id,
            pch_value=tp.trip_pch_value,
            sch_block_hours=tp.sch_block_hours,
            duty_hours=tp.duty_hours,
            tafb_hours=tp.tafb_hours,
            workdays=tp.workdays,
        )
        for tp in sorted(pr.packet.values(), key=lambda t: t.trip_id)
    )

    return _build_day_detail(
        target=target,
        pr=pr,
        trip=trip,
        day_entry=day_entry,
        packet_trip=packet_trip,
        legs=legs,
        actual_block=actual_block,
        events_today=events_today,
        has_override=override is not None,
        saved=saved,
        user_versions=user_versions,
        superseded_seqs=superseded_seqs,
        superseded_by_seq=superseded_by,
        reassign_defaults=reassign_defaults,
        saved_reassign=saved_reassign,
        saved_drop=saved_drop,
        saved_version_deleted=saved_version_deleted,
        reassign_error=reassign_error,
        packet_options=packet_options,
        day_pay_rows=day_pay_rows,
        day_pay_total=day_pay_total,
        duty_on=duty_on,
        duty_off=duty_off,
        duty_hours=duty_hours,
        duty_rig_pch=duty_rig_pch,
        manual_legs_by_seq=manual_legs_by_seq,
    )


def _build_day_pay_rows(
    *,
    pr: PipelineResult,
    trip: Trip | None,
    day_entry: Day | None,
) -> tuple[tuple, Decimal | None]:
    """Phase I.7 — per-day pay breakdown rows from the engine's chunks.

    Filters ChunkResult records to those crediting this calendar date,
    then runs the same categorize → display transform as the Pay
    Breakdown screen. Returns (rows, total_dollars). Total may be None
    when no chunks credit the day (off days with no reassignment).
    """
    from collections import defaultdict as _dd
    from nac_pay.engine import ChunkKind as _CK
    from .services import _categorize as _cat  # local re-bind for clarity

    base_rate = pr.pilot.hourly_rate

    # Compute the set of source_ids that credit this date.
    # Trips with the same trip_id on different dates would otherwise
    # blend together — see _trip_source_id docstring in lower.py.
    target_source_ids: set[str] = set()
    if trip is not None:
        if trip.dates:
            target_source_ids.add(f"{trip.trip_id}@{trip.dates[0].isoformat()}")
        else:
            target_source_ids.add(trip.trip_id)
    if day_entry is not None:
        # Use the exact same source_id the engine stamped on this Day's
        # chunks. day_entry is the pipeline Day matched by date, so reusing
        # _day_source_id keeps the producer/consumer in lock-step — a reserve
        # day's shared line-designator label (e.g. "1021") is date-qualified
        # on both sides so chunks no longer collide across the month.
        from nac_pay.schedule.lower import _day_source_id as _day_sid
        target_source_ids.add(_day_sid(day_entry))
    if not target_source_ids:
        return (), None

    chunks_for_day = [
        c for c in pr.engine_result.per_chunk
        if c.source_id in target_source_ids
    ]
    if not chunks_for_day:
        return (), None

    # Group by (category, multiplier) — same as the monthly breakdown.
    pch_by_key: dict[tuple[str, Decimal], Decimal] = _dd(lambda: Decimal("0"))
    for c in chunks_for_day:
        pch_by_key[(_categorize(c), c.multiplier)] += c.raw_pch

    rows: list[DayPayRow] = []
    for (cat, mult), pch in pch_by_key.items():
        if cat == "Home Study":
            # Same pilot-facing convention as the Pay Breakdown: show
            # module-hours at half-rate.
            display_pch = pch * Decimal("2")
            display_mult = Decimal("0.5")
            rate = base_rate * display_mult
            amount = (display_pch * rate).quantize(_DOLLAR_QUANT, rounding=ROUND_HALF_UP)
            rows.append(DayPayRow(
                category=cat, multiplier=display_mult,
                pch=display_pch, rate=rate,
                base_rate=base_rate, amount=amount,
            ))
        else:
            rate = base_rate * mult
            amount = (pch * rate).quantize(_DOLLAR_QUANT, rounding=ROUND_HALF_UP)
            rows.append(DayPayRow(
                category=cat, multiplier=mult,
                pch=pch, rate=rate,
                base_rate=base_rate, amount=amount,
            ))

    # Sort: higher multipliers first within a category, then by category order.
    def _sort_key(r: DayPayRow) -> tuple[int, str, Decimal]:
        order = _PAY_TYPE_ORDER.index(r.category) if r.category in _PAY_TYPE_ORDER else 99
        return (order, r.category, -r.multiplier)
    rows.sort(key=_sort_key)

    total = sum((r.amount for r in rows), Decimal("0"))
    return tuple(rows), total


def _ical_legs_prefill(ical_legs) -> tuple:
    return tuple(
        {"flight": lg.flight_no, "out": lg.out_local, "in": lg.in_local}
        for lg in (ical_legs or ())
    )


def _build_reassign_defaults(
    user_versions: list,
    correct_seq: int | None,
    restore_pch: Decimal | None = None,
    *,
    ical_legs: tuple = (),
    manual_legs_by_seq: dict | None = None,
) -> ReassignFormDefaults:
    """Pre-fill the form. Fresh: seed the Detailed leg table with the iCal legs
    (pilot adds only what's missing). Correcting: seed with that version's own
    legs (falling back to the iCal legs)."""
    manual_legs_by_seq = manual_legs_by_seq or {}
    ical_prefill = _ical_legs_prefill(ical_legs)
    if correct_seq is None:
        return ReassignFormDefaults(legs=ical_prefill)
    target = next((uv for uv in user_versions if uv.seq == correct_seq), None)
    if target is None:
        return ReassignFormDefaults(legs=ical_prefill)
    from nac_pay.storage import VersionType as _VT
    if target.version_type is _VT.CORRECTION:
        # The route also rejects this, but defensively skip the pre-fill
        # so the UI doesn't suggest an impossible action.
        return ReassignFormDefaults()
    if target.version_type is _VT.DROP:
        # Undo a drop: a CORRECTION superseding the drop reverts the forfeit.
        # The drop itself carries 0 PCH, so pre-fill the ORIGINAL published
        # value (passed in) — saving restores the assignment to it.
        return ReassignFormDefaults(
            version_type="CORRECTION",
            correction_of=target.seq,
            correcting_seq_label=f"v{target.seq}",
            entry_mode="SIMPLE",
            assignment_id=target.assignment_id,
            pch_value=str(restore_pch) if restore_pch is not None else "",
            reason_code="FLOWN",
            is_restore=True,
            legs=ical_prefill,
        )
    target_legs = manual_legs_by_seq.get(target.seq)
    legs_prefill = (
        tuple(
            {"flight": lg.flight, "out": lg.out_local, "in": lg.in_local}
            for lg in target_legs
        )
        if target_legs else ical_prefill
    )
    return ReassignFormDefaults(
        version_type="CORRECTION",
        correction_of=target.seq,
        correcting_seq_label=f"v{target.seq}",
        entry_mode=target.entry_mode.value,
        assignment_id=target.assignment_id,
        legs=legs_prefill,
        pch_value=str(target.pch_value) if target.entry_mode.value == "SIMPLE" else "",
        block_hours=str(target.block_hours) if target.block_hours is not None else "",
        duty_hours=str(target.duty_hours) if target.duty_hours is not None else "",
        tafb_hours=str(target.tafb_hours) if target.tafb_hours is not None else "",
        deadhead_pch=str(target.deadhead_pch) if target.deadhead_pch is not None else "0",
        workdays=str(target.workdays) if target.workdays is not None else "1",
        reason_code=target.reason_code,
        premium_category=target.premium_category,
        notes=target.notes,
    )


def _build_day_detail(
    target: date_t,
    pr: PipelineResult,
    trip: Trip | None,
    day_entry: Day | None,
    packet_trip: TripPairing | None,
    legs: tuple[DayLeg, ...],
    actual_block: Decimal | None,
    events_today: tuple[AppliedEvent, ...],
    has_override: bool = False,
    saved: bool = False,
    user_versions: list | None = None,
    superseded_seqs: set | None = None,
    superseded_by_seq: dict | None = None,
    reassign_defaults: ReassignFormDefaults | None = None,
    saved_reassign: bool = False,
    saved_drop: bool = False,
    saved_version_deleted: bool = False,
    reassign_error: str = "",
    packet_options: tuple = (),
    day_pay_rows: tuple = (),
    day_pay_total: Decimal | None = None,
    duty_on: str | None = None,
    duty_off: str | None = None,
    duty_hours: Decimal | None = None,
    duty_rig_pch: Decimal | None = None,
    manual_legs_by_seq: dict | None = None,
) -> DayDetailData:
    user_versions = user_versions or []
    manual_legs_by_seq = manual_legs_by_seq or {}
    superseded_seqs = superseded_seqs or set()
    superseded_by_seq = superseded_by_seq or {}
    reassign_defaults = reassign_defaults or ReassignFormDefaults()
    weekday_idx = target.weekday()
    date_label = (
        f"{_WEEKDAY_FULL[weekday_idx]}, "
        f"{_MONTH_NAMES[target.month]} {target.day}, {target.year}"
    )

    # Sibling-month navigation (only if both targets are in bundled data).
    prev_iso = next_iso = None
    today = target
    from datetime import timedelta
    prev_candidate = today - timedelta(days=1)
    next_candidate = today + timedelta(days=1)
    if (prev_candidate.year, prev_candidate.month) in _DOC_INDEX or (
        prev_candidate.year == today.year and prev_candidate.month == today.month
    ):
        prev_iso = prev_candidate.isoformat()
    if (next_candidate.year, next_candidate.month) in _DOC_INDEX or (
        next_candidate.year == today.year and next_candidate.month == today.month
    ):
        next_iso = next_candidate.isoformat()

    back_url = f"/calendar?ym={target.year}-{target.month}"

    if trip is not None:
        kind = "trip"
        duty_label = "FLT"
        duty_class = "flt"
        assignment_id = trip.trip_id
        published = trip.published_pch
        effective = trip.effective_pch
        uplift = effective - published
        reason_value = trip.reason_code.value
        premium_value = trip.premium_category.value
        entry_mode_value = trip.entry_mode.value
        reason_label = _REASON_LABELS.get(reason_value, reason_value)
        premium_label = _PREMIUM_LABELS.get(premium_value, premium_value)
        from .services import _premium_multiplier
        premium_multiplier = _premium_multiplier(trip)
        callout_pch: Decimal | None = None
        callout_excess: Decimal | None = None
        sch_block = packet_trip.sch_block_hours if packet_trip else None
        sch_duty = packet_trip.duty_hours if packet_trip else None
        sch_tafb = packet_trip.tafb_hours if packet_trip else None
        block_delta = (
            actual_block - sch_block
            if actual_block is not None and sch_block is not None
            else None
        )
        components = (
            _packet_components(packet_trip) if packet_trip is not None else ()
        )
        in_packet = packet_trip is not None
        packet_trip_id = packet_trip.trip_id if packet_trip else None
        versions = _build_history(
            published=published,
            effective=effective,
            user_versions=user_versions,
            superseded_seqs=superseded_seqs,
            superseded_by_seq=superseded_by_seq,
            packet_by_trip_id=pr.packet,
            original_assignment_id=assignment_id or "",
        )
    elif day_entry is not None:
        kind = "reserve" if day_entry.duty_type is DutyType.RSV else "other"
        is_callout = day_entry.callout_trip_pch is not None
        class_suffix, base_label = _DUTY_DISPLAY.get(
            day_entry.duty_type, ("other", day_entry.duty_type.value)
        )
        duty_label = "CALLOUT" if is_callout else base_label
        duty_class = "flt" if is_callout else class_suffix
        assignment_id = day_entry.label or None
        published = day_entry.pch_value
        effective = (
            max(_DPG, day_entry.callout_trip_pch) if is_callout else day_entry.pch_value
        )
        uplift = effective - published if is_callout else Decimal("0")
        reason_value = day_entry.reason_code.value
        premium_value = day_entry.premium_category.value
        entry_mode_value = "SIMPLE"
        reason_label = _REASON_LABELS.get(reason_value, reason_value)
        premium_label = _PREMIUM_LABELS.get(premium_value, premium_value)
        premium_multiplier = None
        callout_pch = day_entry.callout_trip_pch
        callout_excess = (
            max(Decimal("0"), day_entry.callout_trip_pch - _DPG)
            if is_callout else None
        )
        sch_block = sch_duty = sch_tafb = None
        block_delta = None
        components = ()
        in_packet = False
        packet_trip_id = None
        # Render the pilot-version history on non-trip days too (OFF-day
        # pickups, lifted RSV/PTO/training). The "Original published"
        # baseline is the pre-pickup PCH preserved on the Day (0 for a
        # picked-up OFF day); fall back to the current value if untouched.
        # _build_history returns () when there are no user versions, so a
        # plain reserve/PTO day still shows nothing.
        history_published = (
            day_entry.original_pch
            if day_entry.original_pch is not None
            else published
        )
        versions = _build_history(
            published=history_published,
            effective=effective,
            user_versions=user_versions,
            superseded_seqs=superseded_seqs,
            superseded_by_seq=superseded_by_seq,
            packet_by_trip_id=pr.packet,
            original_assignment_id=assignment_id or "",
        )
    else:
        kind = "off"
        duty_label = "OFF"
        duty_class = "off"
        assignment_id = None
        published = effective = uplift = None
        reason_value = "OFF"
        premium_value = "NONE"
        entry_mode_value = "SIMPLE"
        reason_label = "—"
        premium_label = "—"
        premium_multiplier = None
        callout_pch = callout_excess = None
        sch_block = sch_duty = sch_tafb = None
        block_delta = None
        components = ()
        in_packet = False
        packet_trip_id = None
        versions = ()

    # The Assignment card shows the CURRENT assignment. Reassignment versions
    # are appended to the trip/day but never rewrite trip.trip_id / day.label,
    # so the per-branch assignment_id above is the FA original — which the
    # history block above correctly consumes as its baseline. Now (after the
    # history is built) mirror the calendar's winning-aid logic (highest pch,
    # earliest seq) so the header surfaces the active reassignment's id instead
    # of the stale original. A PCH-only version with no aid keeps the original.
    active_versions_today = [
        uv for uv in user_versions if uv.seq not in superseded_seqs
    ]
    from nac_pay.storage import VersionType as _VT
    is_dropped = any(uv.version_type is _VT.DROP for uv in active_versions_today)
    day_is_callout = (
        day_entry is not None and day_entry.callout_trip_pch is not None
    )
    if is_dropped:
        # A company-approved drop forfeits the assignment. Show 0 effective
        # PCH and a DROPPED tag; keep the FA-original aid + published value
        # visible (the audit trail of what was given up). Don't let the
        # max-winner block below surface a now-irrelevant reassignment aid.
        duty_label = "DROPPED"
        duty_class = "off"
        effective = Decimal("0")
        uplift = Decimal("0")
        premium_multiplier = None
    else:
        if active_versions_today:
            winner = max(
                active_versions_today, key=lambda v: (v.pch_value, v.seq)
            )
            # Only a genuine reassignment to a DIFFERENT id overrides the
            # header. A PCH-only quick edit copies the current id (the reserve
            # line on a callout day), so it must not hijack the displayed
            # assignment — same guard as the calendar winning-aid logic.
            if winner.assignment_id and winner.assignment_id != assignment_id:
                assignment_id = winner.assignment_id
            # If the winning version carries pilot-entered legs, show THOSE in
            # the Legs card (source = Manual) — the pilot's corrected/complete
            # set supersedes the (possibly aged-out) iCal legs.
            if manual_legs_by_seq.get(winner.seq):
                legs = _manual_day_legs(manual_legs_by_seq[winner.seq])
        # On an iCal callout the flown trip IS the assignment — surface
        # callout_trip_id (mirror the calendar's _build_cell) unless a genuine
        # reassignment above already replaced the reserve-line label. The
        # history baseline was built above with the reserve line as "Original".
        if (
            day_is_callout
            and day_entry.callout_trip_id
            and (assignment_id is None or assignment_id == day_entry.label)
        ):
            assignment_id = day_entry.callout_trip_id

    # PCH candidate hierarchy — lay the credited (effective) PCH against the
    # sources it's the greatest of: the reserve/daily guarantee, the assigned
    # (published/callout) value, the flight-op (actual block), and the actual
    # duty rig. Only meaningful when the day was flown (iCal legs present).
    # winner = the candidate equal to the credited value, so the pilot can see
    # *why* the day is worth what it is (e.g. callout 6.08 beats duty-rig 4.95).
    # Scheduled duty window from the packet — the reconstruct fallback when
    # iCal legs are missing (aged out of BlueOne's rolling feed). For a trip
    # day use the matched packet_trip; for a callout look up the flown trip.
    sched_packet = packet_trip
    if sched_packet is None and assignment_id:
        # Resolve the packet trip directly from the active assignment id (FA
        # trip / iCal callout trip / manual callout aid) via subsequence match
        # — independent of the feed, so this engages even when the iCal legs
        # (and the reconciliation match) have aged out of the rolling feed.
        from nac_pay.schedule.apply_actuals import packet_trip_for_aid
        sched_packet = packet_trip_for_aid(assignment_id, pr.packet)
    sched_duty_on = (sched_packet.sched_duty_on or None) if sched_packet else None
    sched_duty_off = (sched_packet.sched_duty_off or None) if sched_packet else None
    sched_duty_rig_pch = (
        sched_packet.duty_hours / Decimal("2")
        if sched_packet is not None and sched_packet.duty_hours > 0
        else None
    )

    pch_candidates: tuple[PchComponent, ...] = ()
    if effective is not None and not is_dropped and (
        legs or sched_duty_rig_pch is not None
    ):
        raw: list[tuple[str, Decimal]] = []
        if day_is_callout or kind == "reserve":
            raw.append(("Reserve (DPG)", _DPG))
        if day_is_callout and callout_pch is not None:
            # Show the callout trip's TRUE published value; the actual duty-rig
            # / block appear as their own candidates, and whichever equals the
            # credited effective PCH is marked the winner. (callout_pch is the
            # already-credited greater-of, so fall back to it for old data that
            # predates callout_published_pch.)
            published_callout = (
                day_entry.callout_published_pch
                if day_entry.callout_published_pch is not None
                else callout_pch
            )
            raw.append(("Assigned trip (published)", published_callout))
        elif published is not None:
            raw.append(("Published", published))
        if actual_block is not None and actual_block > 0:
            raw.append(("Flight-op (actual block)", actual_block))
        if duty_rig_pch is not None:
            raw.append(("Duty-rig (actual)", duty_rig_pch))
        elif sched_duty_rig_pch is not None:
            # No iCal actuals (legs missing) — fall back to the packet's
            # scheduled duty rig so a duty-rig candidate still shows.
            raw.append(("Duty-rig (scheduled)", sched_duty_rig_pch))
        eff_q = effective.quantize(Decimal("0.01"))
        built: list[PchComponent] = []
        seen_winner = False
        for label, p in raw:
            is_win = (not seen_winner) and p.quantize(Decimal("0.01")) == eff_q
            seen_winner = seen_winner or is_win
            built.append(PchComponent(label=label, pch=p, is_winning=is_win))
        pch_candidates = tuple(built)

    return DayDetailData(
        pilot=pr.pilot,
        year=target.year,
        month=target.month,
        day=target.day,
        date_iso=target.isoformat(),
        date_label=date_label,
        weekday_label=_WEEKDAY_SHORT[weekday_idx],
        prev_date_iso=prev_iso,
        next_date_iso=next_iso,
        back_to_calendar_url=back_url,
        kind=kind,
        duty_label=duty_label,
        duty_class=duty_class,
        assignment_id=assignment_id,
        in_packet=in_packet,
        published_pch=published,
        effective_pch=effective,
        pch_uplift=uplift,
        reason_label=reason_label,
        premium_label=premium_label,
        premium_multiplier=premium_multiplier,
        packet_trip_id=packet_trip_id,
        packet_components=components,
        sch_block_hours=sch_block,
        sch_duty_hours=sch_duty,
        sch_tafb_hours=sch_tafb,
        legs=legs,
        actual_block_hours=actual_block,
        block_delta=block_delta,
        versions=versions,
        callout_trip_pch=callout_pch,
        callout_excess=callout_excess,
        applied_events=events_today,
        reason_options=_reason_options(reason_value, kind),
        premium_options=_premium_options(premium_value),
        entry_mode_options=_entry_mode_options(entry_mode_value),
        has_override=has_override,
        editable=(kind != "off"),
        saved=saved,
        reassign_form_defaults=reassign_defaults,
        saved_reassign=saved_reassign,
        saved_drop=saved_drop,
        saved_version_deleted=saved_version_deleted,
        reassign_error=reassign_error,
        packet_trip_options=packet_options,
        day_pay_rows=day_pay_rows,
        day_pay_total=day_pay_total,
        is_dropped=is_dropped,
        duty_on=duty_on,
        duty_off=duty_off,
        duty_hours=duty_hours,
        duty_rig_pch=duty_rig_pch,
        sched_duty_on=sched_duty_on,
        sched_duty_off=sched_duty_off,
        sched_duty_rig_pch=sched_duty_rig_pch,
        pch_candidates=pch_candidates,
    )


def _build_history(
    *,
    published: Decimal,
    effective: Decimal,
    user_versions: list,
    superseded_seqs: set,
    superseded_by_seq: dict,
    packet_by_trip_id: dict | None = None,
    original_assignment_id: str = "",
) -> tuple[DayVersion, ...]:
    """Unified assignment-history list — original + every pilot version.

    Always shows the original (display_seq=0) when any user version
    exists, so the audit context is visible. Each user version appears
    once, with its supersede status. Exactly one row gets is_effective=True
    (the highest non-superseded PCH, with ties going to the earliest seq).

    Phase H: each row also carries any structural data we know about its
    underlying trip — DETAILED-mode inputs from the user version, plus a
    `packet_match` lookup for any assignment_id present in this month's
    packet. The template renders that data inside an expandable details
    section per row.
    """
    if not user_versions:
        return ()

    from nac_pay.storage import VersionType as _VT
    packet_by_trip_id = packet_by_trip_id or {}

    def _packet_lookup(trip_id: str) -> "PacketTripOption | None":
        if not trip_id:
            return None
        tp = packet_by_trip_id.get(trip_id)
        if tp is None:
            return None
        return PacketTripOption(
            trip_id=tp.trip_id,
            pch_value=tp.trip_pch_value,
            sch_block_hours=tp.sch_block_hours,
            duty_hours=tp.duty_hours,
            tafb_hours=tp.tafb_hours,
            workdays=tp.workdays,
        )

    rows: list[DayVersion] = [
        DayVersion(
            seq=0, pch_value=published, label="Original published",
            is_effective=False, source="Original",
            packet_match=_packet_lookup(original_assignment_id),
        )
    ]
    for uv in user_versions:
        is_sup = uv.seq in superseded_seqs
        if uv.version_type is _VT.CORRECTION:
            source = "Pilot correction"
        elif uv.version_type is _VT.RESERVE_CALLOUT:
            source = "Reserve callout"
        else:
            source = "Pilot reassignment"
        rows.append(
            DayVersion(
                seq=uv.seq,
                pch_value=uv.pch_value,
                label=uv.assignment_id or "—",
                is_effective=False,
                source=source,
                user_seq=uv.seq,
                user_version_type=uv.version_type.value,
                correction_of=uv.correction_of,
                is_superseded=is_sup,
                superseded_by_user_seq=superseded_by_seq.get(uv.seq),
                notes=uv.notes,
                created_at=uv.created_at,
                entry_mode=uv.entry_mode.value,
                block_hours=uv.block_hours,
                duty_hours=uv.duty_hours,
                tafb_hours=uv.tafb_hours,
                deadhead_pch=uv.deadhead_pch,
                workdays=uv.workdays,
                packet_match=_packet_lookup(uv.assignment_id),
                reason_code=uv.reason_code,
                premium_category=uv.premium_category,
            )
        )

    # Mark the effective row. A company-approved DROP forfeits the assignment,
    # so it wins regardless of PCH (the latest active drop, by seq). Otherwise
    # the effective row is the highest non-superseded PCH; on a tie the LATEST
    # seq wins (matches the calendar/header winner — a fresh re-entry of the
    # same value becomes effective).
    candidates = [r for r in rows if not r.is_superseded]
    if candidates:
        drops = [r for r in candidates if r.user_version_type == "DROP"]
        if drops:
            winner = max(drops, key=lambda r: r.seq)
        else:
            winner = max(candidates, key=lambda r: (r.pch_value, r.seq))
        rows = [replace(r, is_effective=(r is winner)) for r in rows]
    return tuple(rows)


def _packet_components(packet: TripPairing) -> tuple[PchComponent, ...]:
    """The four §3.E components + deadhead, with the winning component marked."""
    values = {
        "Flight Operation": packet.flight_op_pch,
        "Duty Rig": packet.duty_rig_pch,
        "Trip Rig": packet.trip_rig_pch,
        "Cumulative DPG": packet.cumulative_dpg_pch,
    }
    winning_pch = max(values.values())
    return tuple(
        PchComponent(label=label, pch=val, is_winning=(val == winning_pch))
        for label, val in values.items()
    ) + (
        PchComponent(label="Deadhead", pch=packet.deadhead_pch, is_winning=False),
    )


def _premium_multiplier(trip: Trip) -> Decimal:
    """Resolve the trip's premium multiplier — wraps the labels-table lookup
    so the template doesn't need to know the import path."""
    from nac_pay.schedule.labels import premium_multiplier
    return premium_multiplier(trip.premium_category, trip.custom_multiplier)


def _ordered_subseq(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return False
    i = 0
    for token in haystack:
        if i < len(needle) and needle[i] == token:
            i += 1
        if i == len(needle):
            return True
    return False


# ── Pay breakdown view ─────────────────────────────────────────────────


from collections import defaultdict
from decimal import ROUND_HALF_UP

from nac_pay.engine import ChunkKind, ChunkResult


_DOLLAR_QUANT = Decimal("0.01")


@dataclass(frozen=True)
class EarningRow:
    pay_type: str               # "Regular Pay", "Open Time", etc.
    pch: Decimal                # sum of raw PCH in this row
    rate: Decimal               # multiplied rate (base × multiplier)
    base_rate: Decimal
    multiplier: Decimal
    amount: Decimal             # pch × rate, rounded HALF_UP


@dataclass(frozen=True)
class PayBreakdownData:
    pilot: PilotProfile
    year: int
    month: int
    month_label: str
    available_months: tuple[tuple[int, int, str], ...]

    earning_rows: tuple[EarningRow, ...]
    earned_pch_total: Decimal
    earned_dollars: Decimal

    topup_pch: Decimal
    topup_dollars: Decimal
    total_pch: Decimal           # earned + topup
    total_pay: Decimal           # earned + topup_dollars

    option1_floor: Decimal
    option2_workdays_dpg: Decimal
    option3_earned: Decimal
    winning_option: str          # human label
    winning_key: str             # "floor" / "workdays_dpg" / "earned" — for CSS
    base_monthly_pch: Decimal


# Pay-stub category labels, in display order. Phase I expands the list
# with Overtime / Junior Assignment / Landing / Hostile / and the split
# of Training into its three sub-types (Classroom / SIM / Home Study).
_PAY_TYPE_ORDER: tuple[str, ...] = (
    "Regular Pay",
    "Open Time",
    "Overtime",
    "Junior Assignment",
    "Landing Credit",
    "Hostile Area",
    "Paid Time Off",
    "Sick",
    "Jury Duty",
    "Bereavement",
    "Classroom Train",
    "Simulator Train",
    "Training",          # generic (CLASS/SIM unknown)
    "Home Study",
    "Moving",
    "NRFO",
    "Other",
)


# Premium category .value → display label. Used when a chunk carries a
# premium multiplier but ChunkKind alone doesn't pin the category
# (Phase I fix — Overtime / Landing / Junior etc. previously fell through
# to "Regular Pay 1.5x" which isn't a real contract concept).
_PREMIUM_DISPLAY: dict[str, str] = {
    "OPEN_TIME_MID_MONTH": "Open Time",
    "OPEN_TIME_BID_PERIOD": "Open Time",
    "OVERTIME": "Overtime",
    "JUNIOR_ASSIGNMENT_1ST": "Junior Assignment",
    "JUNIOR_ASSIGNMENT_NTH": "Junior Assignment",
    "LANDING": "Landing Credit",
    "HOSTILE": "Hostile Area",
    "NRFO_SPECIALIZED": "NRFO",
}


def _categorize(chunk: ChunkResult) -> str:
    """Map a ChunkResult to a pay-stub category.

    Priority (Phase I):
    1. Premium chunks (multiplier > 1.0) bucket by ``premium_category``
       — fixes "Regular Pay 1.5x" for Overtime / Landing / Junior Assign
       which were falling through to Regular Pay.
    2. Training splits: ChunkKind.TRAINING + label hint splits into
       Classroom Train / Simulator Train; Home Study has its own kind.
    3. ChunkKind directly maps the rest.
    """
    kind = chunk.kind

    # 1. Premium routing.
    if chunk.multiplier > Decimal("1.0") and chunk.premium_category:
        label = _PREMIUM_DISPLAY.get(chunk.premium_category)
        if label is not None:
            return label

    # 2. Protected / leave categories — these always get their own row
    # regardless of multiplier (PTO at premium is still PTO category).
    if kind is ChunkKind.PTO:
        return "Paid Time Off"
    if kind is ChunkKind.SICK:
        return "Sick"
    if kind is ChunkKind.JURY:
        return "Jury Duty"
    if kind is ChunkKind.BEREAVEMENT:
        return "Bereavement"
    if kind is ChunkKind.TRAINING:
        # Split Classroom vs SIM by inspecting the chunk's label.
        # _CHUNK_KIND_BY_DUTY_TYPE puts CLASS and SIM both under TRAINING
        # so we can't differentiate at the engine level without a label.
        label = (chunk.label or "").upper()
        if "SIM" in label:
            return "Simulator Train"
        if "CLASS" in label or "CLASSROOM" in label:
            return "Classroom Train"
        return "Training"
    if kind is ChunkKind.MOVING:
        return "Moving"
    if kind is ChunkKind.HOME_STUDY:
        return "Home Study"

    # 3. Premium-eligible kinds (when chunk_category wasn't carried).
    if kind is ChunkKind.OPEN_TIME and chunk.multiplier > Decimal("1.0"):
        return "Open Time"
    if kind is ChunkKind.NRFO and chunk.multiplier > Decimal("1.0"):
        return "NRFO"

    # TRIP, RESERVE_DAY (reserve straight time per §5 terminology),
    # OTHER, OPEN_TIME @ 1.0×, NRFO @ 1.0× → Regular Pay.
    return "Regular Pay"


def _build_earning_rows(
    chunks: tuple[ChunkResult, ...],
    base_rate: Decimal,
) -> tuple[EarningRow, ...]:
    """Group chunks by (category, multiplier). Each group yields one row with
    PCH = sum(raw_pch), rate = base × multiplier, amount = PCH × rate.

    Phase I — Home Study display convention: the engine stores Home
    Study chunks as ``raw_pch = module_hours × 0.5`` at multiplier 1.0
    per §3.H. Pilots think of it as hours × half-rate (matches the pay
    stub format), so we display ``hours = raw_pch × 2`` at
    ``rate = base × 0.5``. Math is identical:
        stored: 12.0 PCH × $124.59 × 1.0 = $1,495.08
        display: 24.0 hrs × $62.29     ≈ $1,494.96  ← shown
    Amount stays computed from the displayed PCH × displayed rate so the
    row arithmetic checks out on screen.
    """
    pch_by_key: dict[tuple[str, Decimal], Decimal] = defaultdict(lambda: Decimal("0"))
    for c in chunks:
        key = (_categorize(c), c.multiplier)
        pch_by_key[key] += c.raw_pch

    rows: list[EarningRow] = []
    for (cat, mult), pch in pch_by_key.items():
        if cat == "Home Study":
            # Pilot-facing convention: show module-hours at half-rate.
            display_pch = pch * Decimal("2")
            display_mult = Decimal("0.5")
            rate = base_rate * display_mult
            amount = (display_pch * rate).quantize(_DOLLAR_QUANT, rounding=ROUND_HALF_UP)
            rows.append(
                EarningRow(
                    pay_type=cat,
                    pch=display_pch,
                    rate=rate,
                    base_rate=base_rate,
                    multiplier=display_mult,
                    amount=amount,
                )
            )
        else:
            rate = base_rate * mult
            amount = (pch * rate).quantize(_DOLLAR_QUANT, rounding=ROUND_HALF_UP)
            rows.append(
                EarningRow(
                    pay_type=cat,
                    pch=pch,
                    rate=rate,
                    base_rate=base_rate,
                    multiplier=mult,
                    amount=amount,
                )
            )

    def sort_key(r: EarningRow) -> tuple[int, str, Decimal]:
        order = _PAY_TYPE_ORDER.index(r.pay_type) if r.pay_type in _PAY_TYPE_ORDER else 99
        # Higher multipliers first within a category.
        return (order, r.pay_type, -r.multiplier)

    rows.sort(key=sort_key)
    return tuple(rows)


# ── Pay-stub compare view ─────────────────────────────────────────────


@dataclass(frozen=True)
class MonthlyStubSummary:
    """Two (or more) semi-monthly stubs netted to a monthly view."""

    stubs: tuple[PayStub, ...]
    # Pay-type → (net hours, net current dollars). Multi-row categories
    # (e.g. +80.29 and −32.50 Regular Pay) collapse to one entry.
    by_category: dict[str, tuple[Decimal, Decimal]]
    total_hours: Decimal
    total_dollars: Decimal
    net_pay_sum: Decimal


def combine_stubs(stubs: tuple[PayStub, ...]) -> MonthlyStubSummary:
    by_cat: dict[str, list[Decimal]] = {}    # pay_type → [hours_sum, dollars_sum]
    for stub in stubs:
        for line in stub.earnings:
            slot = by_cat.setdefault(line.pay_type, [Decimal("0"), Decimal("0")])
            slot[0] += line.hours if line.hours is not None else Decimal("0")
            slot[1] += line.current_amount
    summary = {k: (v[0], v[1]) for k, v in by_cat.items()}
    total_hours = sum((h for h, _d in summary.values()), Decimal("0"))
    total_dollars = sum((d for _h, d in summary.values()), Decimal("0"))
    return MonthlyStubSummary(
        stubs=stubs,
        by_category=summary,
        total_hours=total_hours,
        total_dollars=total_dollars,
        net_pay_sum=sum((s.net_pay for s in stubs), Decimal("0")),
    )


_COMPARE_TOL = Decimal("0.50")     # ±$0.50 per category before flagging

# Stub categories that aren't pilot earnings (benefits, taxes). Excluded
# from the comparison totals — the spec notes "Group Term Life is a
# benefit, not PCH".
_NON_EARNING_CATEGORIES: frozenset[str] = frozenset({"Group Term Life"})


class CompareVerdict(StrEnum):
    MATCH = "MATCH"
    TRACKER_OVER = "TRACKER_OVER"      # tracker > company
    TRACKER_UNDER = "TRACKER_UNDER"    # tracker < company
    NO_STUBS = "NO_STUBS"              # no stubs bundled / parsed


@dataclass(frozen=True)
class CategoryCompare:
    pay_type: str
    tracker_pch: Decimal
    tracker_amount: Decimal
    stub_hours: Decimal
    stub_amount: Decimal
    delta_amount: Decimal              # tracker - stub (signed)
    matches: bool


@dataclass(frozen=True)
class StubChip:
    label: str
    pay_date_iso: str
    net_pay: Decimal


@dataclass(frozen=True)
class InspectorStubLine:
    """One row of a stub's Earnings table, raw from the PDF parser."""

    pay_type: str
    hours: Decimal | None
    rate: Decimal | None
    current_amount: Decimal
    ytd_amount: Decimal | None


@dataclass(frozen=True)
class InspectorStub:
    """One parsed pay-stub with everything verbatim from the PDF.

    Surfaced under a collapsible "raw stub data" section on the Compare
    screen so the pilot can study how the company actually itemizes pay
    credit hours across multiple months. The verdict-based compare logic
    is intentionally NOT informed by this — it's a data-collection
    enabler for designing better compare semantics later.
    """

    label: str                        # "05/16/2026 → 05/31/2026"
    period_start_iso: str
    period_end_iso: str
    pay_date_iso: str
    net_pay: Decimal
    total_hours_worked: Decimal | None
    total_hours: Decimal | None
    earnings: tuple[InspectorStubLine, ...]
    source_filename: str              # PDF basename, for cross-reference


@dataclass(frozen=True)
class CompareData:
    pilot: PilotProfile
    year: int
    month: int
    month_label: str
    available_months: tuple[tuple[int, int, str], ...]

    verdict: CompareVerdict
    total_tracker: Decimal
    total_stub: Decimal
    total_delta: Decimal               # tracker - stub
    rows: tuple[CategoryCompare, ...]
    stub_chips: tuple[StubChip, ...]
    note: str                          # optional contextual note
    mpg_advance_netted: bool           # True when the +/-32.50 cancels
    inspector_stubs: tuple[InspectorStub, ...] = ()    # raw per-stub data


# Mapping our internal pay-stub category labels (used by _categorize) to the
# company stub labels. Mostly identical; this exists so deviations
# (e.g. "NRFO") don't accidentally compare against "" on the stub side.
_STUB_LABEL_BY_TRACKER: dict[str, str] = {
    "Regular Pay": "Regular Pay",
    "Open Time": "Open Time",
    "Paid Time Off": "Paid Time Off",
    "Sick": "Sick",
    "Jury Duty": "Jury Duty",
    "Bereavement": "Bereavement",
    "Training": "Training",
    "Moving": "Moving",
    "Home Study": "Home Study",
    "NRFO": "NRFO",
    "Other": "Other",
}


def load_compare(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> CompareData:
    pb = load_pay_breakdown(year, month, user_id)

    stub_paths = stubs_for_user(user_id, year, month)
    if not stub_paths:
        return CompareData(
            pilot=pb.pilot,
            year=year,
            month=month,
            month_label=pb.month_label,
            available_months=available_months(user_id),
            verdict=CompareVerdict.NO_STUBS,
            total_tracker=pb.total_pay,
            total_stub=Decimal("0"),
            total_delta=Decimal("0"),
            rows=(),
            stub_chips=(),
            note="No pay stubs uploaded for this month — add them on the Documents page so this screen can compare.",
            mpg_advance_netted=False,
            inspector_stubs=(),
        )

    stubs = tuple(parse_pay_stub(p) for p in stub_paths)
    summary = combine_stubs(stubs)

    # Tracker per-category dollars (aggregate row.amounts by category label).
    tracker_amount_by_cat: dict[str, Decimal] = {}
    tracker_pch_by_cat: dict[str, Decimal] = {}
    for row in pb.earning_rows:
        tracker_amount_by_cat[row.pay_type] = (
            tracker_amount_by_cat.get(row.pay_type, Decimal("0")) + row.amount
        )
        tracker_pch_by_cat[row.pay_type] = (
            tracker_pch_by_cat.get(row.pay_type, Decimal("0")) + row.pch
        )

    # Sweep both sides to build per-category rows.
    seen_categories: set[str] = set()
    rows: list[CategoryCompare] = []
    for tracker_cat in tracker_amount_by_cat:
        stub_label = _STUB_LABEL_BY_TRACKER.get(tracker_cat, tracker_cat)
        stub_hours, stub_amount = summary.by_category.get(stub_label, (Decimal("0"), Decimal("0")))
        tracker_amt = tracker_amount_by_cat[tracker_cat]
        delta = tracker_amt - stub_amount
        rows.append(
            CategoryCompare(
                pay_type=tracker_cat,
                tracker_pch=tracker_pch_by_cat[tracker_cat],
                tracker_amount=tracker_amt,
                stub_hours=stub_hours,
                stub_amount=stub_amount,
                delta_amount=delta,
                matches=abs(delta) <= _COMPARE_TOL,
            )
        )
        seen_categories.add(stub_label)
    # Stub categories tracker doesn't know about (e.g. extra Earnings rows).
    # Benefits are excluded — not part of pilot earnings.
    for stub_cat, (hours, amount) in summary.by_category.items():
        if stub_cat in seen_categories:
            continue
        if stub_cat in _NON_EARNING_CATEGORIES:
            continue
        if amount == 0 and hours == 0:
            continue
        rows.append(
            CategoryCompare(
                pay_type=stub_cat,
                tracker_pch=Decimal("0"),
                tracker_amount=Decimal("0"),
                stub_hours=hours,
                stub_amount=amount,
                delta_amount=-amount,
                matches=False,
            )
        )

    # Sort: non-matching rows first, then by category display order.
    def sort_key(r: CategoryCompare) -> tuple[int, int, str]:
        order = _PAY_TYPE_ORDER.index(r.pay_type) if r.pay_type in _PAY_TYPE_ORDER else 99
        return (0 if not r.matches else 1, order, r.pay_type)
    rows.sort(key=sort_key)

    total_tracker = sum((r.tracker_amount for r in rows), Decimal("0"))
    total_stub = sum((r.stub_amount for r in rows), Decimal("0"))
    total_delta = total_tracker - total_stub

    if abs(total_delta) <= _COMPARE_TOL:
        verdict = CompareVerdict.MATCH
    elif total_delta > 0:
        verdict = CompareVerdict.TRACKER_OVER
    else:
        verdict = CompareVerdict.TRACKER_UNDER

    # MPG advance detection: look at individual Regular Pay LINES across all
    # stubs. Per-stub sums hide the +/- when both signs share a stub.
    regular_lines = [
        line for stub in stubs for line in stub.earnings
        if line.pay_type == "Regular Pay" and line.hours is not None
    ]
    mpg_netted = (
        any(line.hours < 0 for line in regular_lines)
        and any(line.hours > 0 for line in regular_lines)
    )

    note = ""
    if verdict is CompareVerdict.TRACKER_UNDER:
        note = (
            "Tracker shows less than the company. Most common cause: mid-month "
            "events (reassignments, callouts, open-time pickups) that aren't yet "
            "reflected in the bundled iCal feed."
        )
    elif verdict is CompareVerdict.TRACKER_OVER:
        note = (
            "Tracker shows more than the company. Possible causes: an event was "
            "double-counted, a premium was applied incorrectly, or the company "
            "missed something. Review the category rows below."
        )

    chips = tuple(
        StubChip(
            label=stub.label,
            pay_date_iso=stub.pay_date.isoformat(),
            net_pay=stub.net_pay,
        )
        for stub in stubs
    )

    # Raw per-stub data for the inspector view (collapsible section on
    # /compare). Verbatim from the parser — no normalization, no merging
    # — so the pilot can study how the company actually itemizes pay.
    inspector_stubs = tuple(
        InspectorStub(
            label=stub.label,
            period_start_iso=stub.period_start.isoformat(),
            period_end_iso=stub.period_end.isoformat(),
            pay_date_iso=stub.pay_date.isoformat(),
            net_pay=stub.net_pay,
            total_hours_worked=stub.total_hours_worked,
            total_hours=stub.total_hours,
            earnings=tuple(
                InspectorStubLine(
                    pay_type=line.pay_type,
                    hours=line.hours,
                    rate=line.rate,
                    current_amount=line.current_amount,
                    ytd_amount=line.ytd_amount,
                )
                for line in stub.earnings
            ),
            source_filename=Path(stub.source_path).name,
        )
        for stub in stubs
    )

    return CompareData(
        pilot=pb.pilot,
        year=year,
        month=month,
        month_label=pb.month_label,
        available_months=available_months(user_id),
        verdict=verdict,
        total_tracker=total_tracker,
        total_stub=total_stub,
        total_delta=total_delta,
        rows=tuple(rows),
        stub_chips=chips,
        note=note,
        mpg_advance_netted=mpg_netted,
        inspector_stubs=inspector_stubs,
    )


# ── Discrepancies queue (§13 #6) ──────────────────────────────────────


class DiscrepancyKind(StrEnum):
    PACKET_VALIDATION = "PACKET_VALIDATION"
    COMPARE_MISMATCH = "COMPARE_MISMATCH"
    UNMATCHED_TRIP = "UNMATCHED_TRIP"


class DiscrepancySeverity(StrEnum):
    OWED_MONEY = "OWED_MONEY"           # tracker > stub: company underpaid
    INVESTIGATION = "INVESTIGATION"     # tracker < stub: likely missing data on our side
    REVIEW = "REVIEW"                   # needs pilot input (categorize / triage)
    INFO = "INFO"                       # advisory only


_SEVERITY_PRIORITY: dict[DiscrepancySeverity, int] = {
    DiscrepancySeverity.OWED_MONEY: 0,
    DiscrepancySeverity.INVESTIGATION: 1,
    DiscrepancySeverity.REVIEW: 2,
    DiscrepancySeverity.INFO: 3,
}


@dataclass(frozen=True)
class DiscrepancyItem:
    kind: DiscrepancyKind
    severity: DiscrepancySeverity
    title: str
    detail: str
    date: date_t | None
    trip_id: str | None
    money_impact: Decimal | None        # signed; positive = tracker > stub
    action_label: str
    action_url: str


@dataclass(frozen=True)
class DiscrepanciesData:
    pilot: PilotProfile
    year: int
    month: int
    month_label: str
    available_months: tuple[tuple[int, int, str], ...]

    items: tuple[DiscrepancyItem, ...]
    counts_by_severity: dict[str, int]
    total_money_impact: Decimal


def load_discrepancies(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> DiscrepanciesData:
    pr = _pipeline(year, month, user_id)
    items: list[DiscrepancyItem] = []
    ym = f"{year}-{month}"

    # 1. Packet validation flags (§9)
    for v in pr.validation_discrepancies:
        items.append(
            DiscrepancyItem(
                kind=DiscrepancyKind.PACKET_VALIDATION,
                severity=DiscrepancySeverity.REVIEW,
                title=f"Trip {v.trip_id}: {v.field} mismatch",
                detail=(
                    f"Packet printed {v.printed}, recomputed {v.recomputed} "
                    f"(Δ {v.delta:+}). Check packet printing vs §3.E formula."
                ),
                date=None,
                trip_id=v.trip_id,
                money_impact=None,
                action_label="View pay breakdown",
                action_url=f"/pay?ym={ym}",
            )
        )

    # 2. Compare mismatches (only when stubs exist)
    if stubs_for_user(user_id, year, month):
        compare = load_compare(year, month, user_id)
        if compare.verdict is not CompareVerdict.NO_STUBS:
            for row in compare.rows:
                if row.matches:
                    continue
                if row.delta_amount > 0:
                    severity = DiscrepancySeverity.OWED_MONEY
                    title = (
                        f"{row.pay_type}: tracker says you're owed "
                        f"${row.delta_amount:.2f}"
                    )
                else:
                    severity = DiscrepancySeverity.INVESTIGATION
                    title = (
                        f"{row.pay_type}: tracker shows "
                        f"${-row.delta_amount:.2f} less than the company"
                    )
                items.append(
                    DiscrepancyItem(
                        kind=DiscrepancyKind.COMPARE_MISMATCH,
                        severity=severity,
                        title=title,
                        detail=(
                            f"Tracker: {row.tracker_pch:.2f} PCH = "
                            f"${row.tracker_amount:,.2f}; "
                            f"Stub: {row.stub_hours:.2f} hrs = "
                            f"${row.stub_amount:,.2f}."
                        ),
                        date=None,
                        trip_id=None,
                        money_impact=row.delta_amount,
                        action_label="Open compare",
                        action_url=f"/compare?ym={ym}",
                    )
                )

    # 3. Unmatched iCal trips (apply_actuals UNMATCHED_TRIP_REVIEW) — but a
    # date the pilot has already categorized with an active version (e.g. a
    # recorded callout for legs the feed couldn't match to the packet) is
    # resolved, so suppress the review for those dates.
    from nac_pay.schedule import AppliedEventKind
    from nac_pay.storage import UserAssignmentVersionStore, active_versions
    addressed_dates: set[str] = set()
    for date_iso, vs in UserAssignmentVersionStore(
        user_id=user_id,
    ).list_for_month(year, month).items():
        if active_versions(vs)[0]:
            addressed_dates.add(date_iso)
    for ev in pr.applied_events:
        if ev.kind is not AppliedEventKind.UNMATCHED_TRIP_REVIEW:
            continue
        if ev.date.isoformat() in addressed_dates:
            continue
        items.append(
            DiscrepancyItem(
                kind=DiscrepancyKind.UNMATCHED_TRIP,
                severity=DiscrepancySeverity.REVIEW,
                title=f"Unmatched flight on {ev.date.isoformat()}",
                detail=ev.detail,
                date=ev.date,
                trip_id=ev.trip_id,
                money_impact=None,
                action_label="View day",
                action_url=f"/day/{ev.date.isoformat()}",
            )
        )

    def sort_key(it: DiscrepancyItem) -> tuple[int, Decimal, str]:
        prio = _SEVERITY_PRIORITY[it.severity]
        magnitude = -abs(it.money_impact) if it.money_impact is not None else Decimal("0")
        return (prio, magnitude, it.title)

    items.sort(key=sort_key)

    counts = {s.value: 0 for s in DiscrepancySeverity}
    total_impact = Decimal("0")
    for it in items:
        counts[it.severity.value] += 1
        if it.money_impact is not None:
            total_impact += it.money_impact

    return DiscrepanciesData(
        pilot=pr.pilot,
        year=year,
        month=month,
        month_label=f"{_MONTH_NAMES[month]} {year}",
        available_months=available_months(user_id),
        items=tuple(items),
        counts_by_severity=counts,
        total_money_impact=total_impact,
    )


def load_pay_breakdown(
    year: int,
    month: int,
    user_id: str = DEFAULT_USER_ID,
) -> PayBreakdownData:
    pr = _pipeline(year, month, user_id)
    r = pr.engine_result
    base_rate = pr.pilot.hourly_rate

    rows = _build_earning_rows(r.per_chunk, base_rate)
    earned_pch = sum((row.pch for row in rows), Decimal("0"))
    earned_dollars_from_rows = sum(
        (row.amount for row in rows), Decimal("0")
    )

    return PayBreakdownData(
        pilot=pr.pilot,
        year=pr.year,
        month=pr.month,
        month_label=f"{_MONTH_NAMES[pr.month]} {pr.year}",
        available_months=available_months(user_id),
        earning_rows=rows,
        earned_pch_total=earned_pch,
        # Display the row-sum so the table footer matches the displayed rows.
        # The engine's r.earned_dollars uses raw-accumulator rounding which
        # may differ by a cent or two; we surface that in tests but the
        # user-facing total here is row-anchored to keep the pay-stub
        # PCH × rate identity visible.
        earned_dollars=earned_dollars_from_rows,
        topup_pch=r.topup_pch,
        topup_dollars=r.topup_dollars,
        total_pch=earned_pch + r.topup_pch,
        total_pay=earned_dollars_from_rows + r.topup_dollars,
        option1_floor=r.option1_floor,
        option2_workdays_dpg=r.option2_workdays_dpg,
        option3_earned=r.option3_earned,
        winning_option=_winning_option_label(r.winning_option),
        winning_key=r.winning_option.value,
        base_monthly_pch=r.base_monthly_pch,
    )


def _build_cell(
    d: date_t,
    month: int,
    trip_by_date: dict[date_t, Trip],
    day_by_date: dict[date_t, Day],
    user_reassignment_count: int = 0,
    new_assignment_id: str | None = None,
    premium_label: str | None = None,
    base_rate: Decimal | None = None,
    has_user_callout: bool = False,
) -> CalendarCell:
    in_month = d.month == month
    is_weekend = d.weekday() >= 5

    def _pay_for(pch: Decimal | None, multiplier: Decimal) -> Decimal | None:
        """Phase I.5 — per-day dollar value rounded to nearest whole dollar."""
        if pch is None or base_rate is None or pch <= 0:
            return None
        return (pch * base_rate * multiplier).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )

    trip = trip_by_date.get(d)
    day = day_by_date.get(d)

    from nac_pay.schedule.labels import ReasonCode as _RC
    _dropped = (
        (trip is not None and trip.reason_code is _RC.VOLUNTARY_DROP)
        or (day is not None and day.reason_code is _RC.VOLUNTARY_DROP)
    )
    if _dropped:
        # Company-approved drop: the assignment was forfeited. Show a DROPPED
        # tag with the FA-original aid still visible (audit), 0 PCH, no pay.
        original_aid = (trip.trip_id if trip is not None else (day.label or None))
        return CalendarCell(
            date=d,
            in_month=in_month,
            is_weekend=is_weekend,
            assignment_id=original_aid,
            duty_label="DROPPED",
            duty_class="off",
            pch=None,
            has_callout=False,
            is_reassigned=False,
            user_reassignment_count=user_reassignment_count,
            premium_label=None,
            pay_dollars=None,
            is_dropped=True,
        )

    if trip is not None:
        from .services import _premium_multiplier
        mult = _premium_multiplier(trip)
        return CalendarCell(
            date=d,
            in_month=in_month,
            is_weekend=is_weekend,
            assignment_id=trip.trip_id,
            duty_label="FLT",
            duty_class="flt",
            pch=trip.effective_pch,
            has_callout=has_user_callout,
            is_reassigned=len(trip.versions) > 0,
            user_reassignment_count=user_reassignment_count,
            new_assignment_id=new_assignment_id,
            premium_label=premium_label,
            pay_dollars=_pay_for(trip.effective_pch, mult),
        )

    if day is not None:
        class_suffix, label = _DUTY_DISPLAY.get(
            day.duty_type, ("other", day.duty_type.value)
        )
        # iCal-derived callout (callout_trip_pch) flips the cell to a full
        # CALLOUT look; a manual reserve-callout version only lights the bolt
        # and keeps the RSV label (pay flows through the reassignment path).
        is_callout = day.callout_trip_pch is not None
        display_class = "flt" if is_callout else class_suffix
        display_label = "CALLOUT" if is_callout else label
        from nac_pay.engine.constants import DPG
        from nac_pay.schedule.labels import premium_multiplier as _pm
        pch_display = (
            max(DPG, day.callout_trip_pch) if is_callout else day.pch_value
        )
        mult = _pm(day.premium_category, day.custom_multiplier)
        # On an iCal callout, surface the flown trip id (e.g. 720/1780) as the
        # bold "new" assignment over the subtle reserve line (e.g. 1021), the
        # same treatment a pilot reassignment gets. A manually-supplied
        # new_assignment_id (the user-version path) still wins if present.
        new_aid = new_assignment_id or (day.callout_trip_id if is_callout else None)
        return CalendarCell(
            date=d,
            in_month=in_month,
            is_weekend=is_weekend,
            assignment_id=day.label or None,
            duty_label=display_label,
            duty_class=display_class,
            pch=pch_display,
            has_callout=is_callout or has_user_callout,
            is_reassigned=False,
            user_reassignment_count=user_reassignment_count,
            new_assignment_id=new_aid,
            premium_label=premium_label,
            pay_dollars=_pay_for(pch_display, mult),
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
        user_reassignment_count=user_reassignment_count,
    )
