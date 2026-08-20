"""Section 3.E: trip PCH = greatest of the four components, plus deadhead.

Used both at engine-input prep time and by the §9 monthly validation check.
Also exposes §3.E.1.b "reassignment greater-of" — pay the max of the
originally-published PCH and any later recomputation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .constants import DPG, TRIP_RIG_DIVISOR


@dataclass(frozen=True)
class TripPchComponents:
    flight_op: Decimal
    duty_rig: Decimal
    trip_rig: Decimal
    cumulative_dpg: Decimal
    deadhead: Decimal = Decimal("0")

    @property
    def trip_pch(self) -> Decimal:
        return max(self.flight_op, self.duty_rig, self.trip_rig, self.cumulative_dpg) + self.deadhead


def components_from_times(
    block_hours: Decimal,
    duty_hours: Decimal,
    tafb_hours: Decimal,
    workdays: int,
    deadhead: Decimal = Decimal("0"),
) -> TripPchComponents:
    return TripPchComponents(
        flight_op=block_hours,
        duty_rig=duty_hours / Decimal("2"),
        trip_rig=tafb_hours / TRIP_RIG_DIVISOR,
        cumulative_dpg=Decimal(workdays) * DPG,
        deadhead=deadhead,
    )


def recompute_pch_from_times(
    block_hours: Decimal,
    duty_hours: Decimal,
    tafb_hours: Decimal,
    workdays: int = 1,
    deadhead: Decimal = Decimal("0"),
) -> Decimal:
    """Detailed-mode entry helper: compute a single trip's PCH from raw
    times via §3.E. Used by the pilot reassignment form (Phase G) and
    anywhere else that needs a quick trip-level recompute without
    constructing the full ``TripPchComponents``.

    ``workdays`` defaults to 1 because most pilot-driven reassignments
    are entered one day at a time."""
    return components_from_times(
        block_hours=block_hours,
        duty_hours=duty_hours,
        tafb_hours=tafb_hours,
        workdays=workdays,
        deadhead=deadhead,
    ).trip_pch


def deadhead_pch_from_times(
    dh_block_hours: Decimal,
    duty_hours: Decimal,
    tafd_hours: Decimal = Decimal("0"),
    duty_periods: int = 1,
) -> Decimal:
    """§3.E.1.d: deadhead PCH = the greater of three legs.

    (i)   fifty percent of all scheduled Deadhead block time;
    (ii)  100% of the Duty Rig (§3.E.2) for all Duty Periods involving
          any Deadhead — 1 PCH per 2 hours on duty, floored at DPG per
          Duty Period;
    (iii) 100% of the Trip Rig (§3.E.3) for all Time Away From Domicile
          when Deadheading (1 PCH per 4.90 hours of TAFD).

    §7.C.2 makes report/release time part of a Deadhead Assignment, so
    ``duty_hours`` must already include the report/release pads — the
    caller owns the duty-window derivation. ``tafd_hours`` defaults to 0
    for a same-day deadhead where no TAFD measure applies.

    Live calibration (2026-08-20 MIA→DFW→ANC return): the company's
    published 6.22 is exactly leg (ii) computed WITHOUT report/release
    (12.45 h dep→arr ÷ 2, rounded down) — padding the duty window per
    §7.C.2 yields 6.85, which §3.E.1.b then pays as the greater value.

    Returns the raw (unquantized) PCH like the other helpers here;
    quantization to 2 dp happens at the compute/display layer.
    """
    half_block = dh_block_hours / Decimal("2")
    duty_rig = max(duty_hours / Decimal("2"), Decimal(duty_periods) * DPG)
    trip_rig = tafd_hours / TRIP_RIG_DIVISOR
    return max(half_block, duty_rig, trip_rig)


def effective_trip_pch_after_reassignment(
    original_published: Decimal,
    *recomputed_candidates: Decimal,
) -> Decimal:
    """§3.E.1.b: pay the greater of original published PCH and any recomputation.

    The recomputation may come from a reassignment, reroute, cancellation,
    deadhead, or duty extension. The pilot is protected from a reduction
    (you keep the original if it's higher) and benefits from any uplift
    (you get the new if it's higher).

    Example (duty extension from spec §3.E):
        original PCH 5, duty extends 8→12 hrs → recomputed duty_rig = 6
        effective_trip_pch_after_reassignment(5, 6) == 6

    Example (protection):
        original PCH 5.33, reroute drops it to 4.00
        effective_trip_pch_after_reassignment(Decimal("5.33"), Decimal("4.00")) == 5.33

    Accepts any number of recomputed candidates so a chain of mid-month
    changes (e.g., reroute then deadhead) folds correctly.
    """
    if not recomputed_candidates:
        return original_published
    return max(original_published, *recomputed_candidates)
