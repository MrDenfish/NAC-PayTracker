"""Convert a parser ``PilotMonthSchedule`` into a schedule ``Month``.

This is the bridge from raw Final Award grid data to the schedule-layer
domain model. The output is the **awarded baseline** — no mid-month
events applied. Reassignments, callouts, drops, and pickups land on top
later when packet / iCal data is wired in.

Scope notes:

- Each scheduled day becomes one ``Trip`` (for FLT) or one ``Day`` (for
  RSV / PTO / SICK / TRAINING / etc.). Multi-calendar-day pairings are
  not detected here — without packet data we can't tell whether two
  consecutive same-assignment FLT days are one multi-day pairing
  (1 duty period) or two single-day pairings (2 duty periods). Treating
  each calendar day as 1 workday is the safe approximation; if it
  matters, the packet integration will refine it.

- Unknown duty tokens (e.g. ``"24"`` / ``"DAY"`` for the carve-outs PBG
  shows in May) are reported back via ``ConversionWarning`` rather than
  silently dropped. The caller can decide to log, raise, or proceed.

- ``is_off`` cells are skipped entirely (they don't contribute to either
  chunk or floor in the engine).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nac_pay.parsers import PilotMonthSchedule

from .labels import DutyType, EntryMode, ReasonCode
from .models import Day, Month, PilotProfile, Trip


@dataclass(frozen=True)
class ConversionWarning:
    date_iso: str
    issue: str          # e.g. "unknown-duty-type", "missing-pch"
    raw_cell: str       # original tokens for debugging


# Master Schedule string → enum mapping.
_DUTY_TYPE_BY_STR: dict[str, DutyType] = {
    "FLT": DutyType.FLT,
    "RSV": DutyType.RSV,
    "PTO": DutyType.PTO,
    "FMLA": DutyType.FMLA,
    "CLASS": DutyType.CLASS,
    "SIM": DutyType.SIM,
    "DH": DutyType.DH,
    "VX": DutyType.VX,
    "OFF": DutyType.OFF,
    "RGS": DutyType.CLASS,        # Recurrent Ground School → classroom
    "MOVING": DutyType.MOVING,
    "TAXI": DutyType.TAXI,
}

# Default reason code per duty type (pilot can override later in the UI).
_REASON_DEFAULT_BY_DUTY: dict[DutyType, ReasonCode] = {
    DutyType.FLT: ReasonCode.FLOWN,
    DutyType.RSV: ReasonCode.FLOWN,           # sit reserve = "flown" the reserve day
    DutyType.PTO: ReasonCode.PTO,
    DutyType.FMLA: ReasonCode.FMLA,
    DutyType.CLASS: ReasonCode.TRAINING,
    DutyType.SIM: ReasonCode.TRAINING,
    DutyType.DH: ReasonCode.FLOWN,            # deadhead is flown duty
    DutyType.VX: ReasonCode.OFF,              # vacation X-out — engine treats as off
    DutyType.OFF: ReasonCode.OFF,
    DutyType.MOVING: ReasonCode.MOVING,
    DutyType.TAXI: ReasonCode.FLOWN,
}


def month_from_master_schedule(
    sched: PilotMonthSchedule,
    pilot: PilotProfile,
) -> tuple[Month, tuple[ConversionWarning, ...]]:
    """Lift the parsed grid into a ``Month`` for the supplied pilot.

    Returns the Month plus any per-cell warnings. The Month is always
    well-formed (skipping is the safe default); warnings let the caller
    surface data quality issues to the pilot for review.
    """
    trips: list[Trip] = []
    days: list[Day] = []
    warnings: list[ConversionWarning] = []

    for cell in sched.days:
        if cell.is_off:
            continue

        duty_str = (cell.duty_type or "").upper()
        duty = _DUTY_TYPE_BY_STR.get(duty_str)
        if duty is None:
            warnings.append(
                ConversionWarning(
                    date_iso=cell.date.isoformat(),
                    issue="unknown-duty-type",
                    raw_cell=f"aid={cell.assignment_id!r} duty={cell.duty_type!r} "
                             f"pch={cell.pch_value}",
                )
            )
            continue

        reason = _REASON_DEFAULT_BY_DUTY.get(duty, ReasonCode.FLOWN)
        pch = cell.pch_value if cell.pch_value is not None else Decimal("0")
        if cell.pch_value is None and reason is not ReasonCode.OFF:
            warnings.append(
                ConversionWarning(
                    date_iso=cell.date.isoformat(),
                    issue="missing-pch",
                    raw_cell=f"aid={cell.assignment_id!r} duty={cell.duty_type!r}",
                )
            )

        if duty is DutyType.FLT:
            trips.append(
                Trip(
                    trip_id=cell.assignment_id or f"FLT-{cell.date.isoformat()}",
                    published_pch=pch,
                    reason_code=reason,
                    workdays=1,
                    entry_mode=EntryMode.SIMPLE,
                    label=f"{cell.assignment_id} on {cell.date.isoformat()}",
                    dates=(cell.date,),
                )
            )
        else:
            days.append(
                Day(
                    date=cell.date,
                    duty_type=duty,
                    pch_value=pch,
                    reason_code=reason,
                    workdays=1 if reason is not ReasonCode.OFF else 0,
                    label=cell.assignment_id or "",
                )
            )

    month = Month(
        pilot=pilot,
        year=sched.year,
        month=sched.month,
        line_value=sched.line_value,
        trips=tuple(trips),
        days=tuple(days),
    )
    return month, tuple(warnings)
