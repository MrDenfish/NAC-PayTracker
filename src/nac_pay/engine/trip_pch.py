"""Section 3.E: trip PCH = greatest of the four components, plus deadhead.

Used both at engine-input prep time and by the §9 monthly validation check.
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
