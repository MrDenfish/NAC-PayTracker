"""§9 Monthly Validation Check.

For each trip in a packet, recompute the four §3.E PCH components from
the printed raw times (block / duty / TAFB / workdays / deadhead) and
diff them against the packet's own printed component values. Then check
that the printed TRIP PCH VALUE equals ``max(4 components) + deadhead``.

Two independent safety nets in one pass: catches a packet error *and*
a bug in our own formula. A clean packet should yield zero discrepancies.

Runs once per packet load — not on the pay path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nac_pay.engine.trip_pch import components_from_times

from .trip_pairing_packet import TripPairing

_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class ValidationDiscrepancy:
    trip_id: str
    field: str
    printed: Decimal
    recomputed: Decimal
    delta: Decimal               # printed - recomputed
    page_index: int

    def __str__(self) -> str:
        return (
            f"{self.trip_id} p.{self.page_index + 1} {self.field}: "
            f"printed={self.printed} recomputed={self.recomputed} "
            f"Δ={self.delta}"
        )


def validate_trip_pairing_packet(
    packet: dict[str, TripPairing],
    tolerance: Decimal = _TOLERANCE,
) -> list[ValidationDiscrepancy]:
    """Return every component / trip-value mismatch beyond ``tolerance``."""
    out: list[ValidationDiscrepancy] = []
    for trip in packet.values():
        out.extend(_validate_one(trip, tolerance))
    return out


def _validate_one(
    trip: TripPairing,
    tol: Decimal,
) -> list[ValidationDiscrepancy]:
    recomputed = components_from_times(
        block_hours=trip.sch_block_hours,
        duty_hours=trip.duty_hours,
        tafb_hours=trip.tafb_hours,
        workdays=trip.workdays,
        deadhead=trip.deadhead_pch,
    )
    diffs: list[ValidationDiscrepancy] = []

    def check(field: str, printed: Decimal, computed: Decimal) -> None:
        delta = printed - computed
        if abs(delta) > tol:
            diffs.append(
                ValidationDiscrepancy(
                    trip_id=trip.trip_id,
                    field=field,
                    printed=printed,
                    recomputed=computed,
                    delta=delta,
                    page_index=trip.page_index,
                )
            )

    check("flight_op_pch", trip.flight_op_pch, recomputed.flight_op)
    check("duty_rig_pch", trip.duty_rig_pch, recomputed.duty_rig)
    check("trip_rig_pch", trip.trip_rig_pch, recomputed.trip_rig)
    check("cumulative_dpg_pch", trip.cumulative_dpg_pch, recomputed.cumulative_dpg)

    # TRIP PCH VALUE = max(4 recomputed components) + deadhead.
    # (Engine formula: trip_pch_components.trip_pch uses this same shape.)
    recomputed_trip_val = recomputed.trip_pch
    check("trip_pch_value", trip.trip_pch_value, recomputed_trip_val)

    return diffs
