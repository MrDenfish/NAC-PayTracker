"""Data loader: full pipeline → DashboardData ready for templating.

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

We don't persist anything yet — the docs/ directory is the authoritative
source. In-memory cache keyed on (year, month, pilot_code) so screen
navigation stays snappy without re-parsing PDFs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    Month,
    PilotProfile,
    Position,
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
        None,  # no iCal sample for May
    ),
    (2026, 6): (
        DOCS_ROOT / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf",
        DOCS_ROOT / "JUNE 2026 Trip Pairing Packet.pdf",
        DOCS_ROOT / "iCal_schedule_feed.ics",
    ),
}


@dataclass(frozen=True)
class DashboardData:
    """Everything the Dashboard template needs from the pipeline."""

    pilot: PilotProfile
    year: int
    month: int
    month_label: str                  # "June 2026"
    available_months: tuple[tuple[int, int, str], ...]

    # Engine outputs
    line_value: Decimal
    base_monthly_pch: Decimal
    winning_option: str               # human label
    option1_floor: Decimal
    option2_workdays_dpg: Decimal
    option3_earned: Decimal
    earned_dollars: Decimal
    topup_pch: Decimal
    topup_dollars: Decimal
    total_pay: Decimal

    # Status strip
    fa_loaded: bool
    packet_loaded: bool
    feed_loaded: bool
    packet_trip_count: int
    feed_event_count: int

    # Activity / discrepancies
    applied_events: tuple[AppliedEvent, ...] = ()
    validation_discrepancies: tuple[ValidationDiscrepancy, ...] = ()


_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


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
def load_dashboard(
    year: int,
    month: int,
    pilot_code: str = "DFI",
) -> DashboardData:
    """Run the full pipeline and return a DashboardData. Cached per call."""
    paths = _DOC_INDEX.get((year, month))
    if paths is None:
        raise ValueError(f"No data bundled for {_MONTH_NAMES[month]} {year}")
    fa_path, packet_path, feed_path = paths

    pilot = DEFAULT_PILOT if pilot_code == "DFI" else DEFAULT_PILOT  # multi-pilot TBD

    # FA → baseline
    fa_grids = parse_master_schedule(str(fa_path))
    sched = fa_grids.get(pilot_code)
    if sched is None:
        raise ValueError(
            f"Pilot {pilot_code} not found in {fa_path.name}. "
            f"Available: {sorted(fa_grids)}"
        )
    baseline, _warnings = month_from_master_schedule(sched, pilot)

    # Packet → validation
    packet = parse_trip_pairing_packet(str(packet_path))
    validation = tuple(validate_trip_pairing_packet(packet))

    # iCal (optional)
    feed: ParsedFeed | None = None
    reconciliation = None
    if feed_path is not None and feed_path.exists():
        feed = parse_ical_feed(str(feed_path))
        reconciliation = reconcile_feed_to_packet(feed, packet)

    # Apply actuals
    if reconciliation is not None:
        updated, applied = apply_actuals_to_month(baseline, reconciliation)
    else:
        updated, applied = baseline, ()

    # Engine
    engine_in = lower_month(updated)
    result: EngineResult = compute_pay(engine_in)

    return DashboardData(
        pilot=pilot,
        year=year,
        month=month,
        month_label=f"{_MONTH_NAMES[month]} {year}",
        available_months=available_months(),
        line_value=updated.line_value,
        base_monthly_pch=result.base_monthly_pch,
        winning_option=_winning_option_label(result.winning_option),
        option1_floor=result.option1_floor,
        option2_workdays_dpg=result.option2_workdays_dpg,
        option3_earned=result.option3_earned,
        earned_dollars=result.earned_dollars,
        topup_pch=result.topup_pch,
        topup_dollars=result.topup_dollars,
        total_pay=result.total_pay,
        fa_loaded=True,
        packet_loaded=True,
        feed_loaded=feed is not None,
        packet_trip_count=len(packet),
        feed_event_count=feed.total_events if feed else 0,
        applied_events=tuple(applied),
        validation_discrepancies=validation,
    )


def _winning_option_label(opt: WinningOption) -> str:
    return {
        WinningOption.FLOOR: "Guarantee floor",
        WinningOption.WORKDAYS_DPG: "Workdays × DPG",
        WinningOption.EARNED: "Sum earned",
    }[opt]
