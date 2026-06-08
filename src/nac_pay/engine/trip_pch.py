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
