"""Re-prove the §6 worked checks via the schedule layer.

Each test constructs a ``Month`` domain object, lowers it to an
``EngineInput``, runs the engine, and asserts the same totals as
``tests/engine/test_worked_examples.py``. This proves the end-to-end
path: domain model + lowering + engine compose correctly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from nac_pay.engine import WinningOption, compute_pay
from nac_pay.schedule import (
    Day,
    DutyType,
    Month,
    PilotProfile,
    Position,
    PremiumCategory,
    ReasonCode,
    Trip,
    lower_month,
)

D = Decimal


def _pilot(rate: str = "100") -> PilotProfile:
    return PilotProfile(
        pilot_id="MEZ",
        name="Test Pilot",
        position=Position.FO,
        hourly_rate=D(rate),
    )


def _approx(actual: Decimal, expected: Decimal, tol: str = "0.01") -> None:
    assert abs(actual - expected) <= Decimal(tol), f"{actual} != {expected} (±{tol})"


# ── 1. Normal month — line 68, flown fully ──────────────────────────────
def test_lowering_normal_month():
    month = Month(
        pilot=_pilot(),
        year=2026,
        month=5,
        line_value=D("68"),
        trips=(
            Trip(
                trip_id="MAY-LINE",
                published_pch=D("68"),
                reason_code=ReasonCode.FLOWN,
                workdays=17,
            ),
        ),
    )
    result = compute_pay(lower_month(month))
    assert result.base_monthly_pch == D("68.00")
    assert result.winning_option in (WinningOption.FLOOR, WinningOption.EARNED)
    _approx(result.total_pay, D("68") * D("100"))


# ── 2. Light protected month — floor 65, flew 40 + sick 10 ──────────────
def test_lowering_light_protected_month():
    sick_day = Day(
        date=date(2026, 5, 15),
        duty_type=DutyType.FLT,
        pch_value=D("10"),
        reason_code=ReasonCode.SICK,
        workdays=3,
    )
    month = Month(
        pilot=_pilot(),
        year=2026,
        month=5,
        line_value=D("50"),  # floor = max(50, 65) = 65
        trips=(
            Trip(
                trip_id="FLOWN-PART",
                published_pch=D("40"),
                reason_code=ReasonCode.FLOWN,
                workdays=10,
            ),
        ),
        days=(sick_day,),
    )
    result = compute_pay(lower_month(month))
    assert result.option1_floor == D("65.00")
    assert result.option3_earned == D("50.00")
    assert result.base_monthly_pch == D("65.00")
    assert result.winning_option == WinningOption.FLOOR
    assert result.topup_pch == D("15.00")
    _approx(result.total_pay, D("65") * D("100"))


# ── 3. Reserve callout — 17 reserve days, day-5 callout 4.50 ────────────
def test_lowering_reserve_callout():
    reserves = tuple(
        Day(
            date=date(2026, 5, d),
            duty_type=DutyType.RSV,
            pch_value=D("3.82"),
            reason_code=ReasonCode.FLOWN,
            workdays=1,
            label=f"RSV-{d}",
        )
        for d in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)
    )
    callout_day = Day(
        date=date(2026, 5, 5),
        duty_type=DutyType.RSV,
        pch_value=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        callout_trip_pch=D("4.50"),
        label="RSV-5-CALLOUT",
    )

    month = Month(
        pilot=_pilot(),
        year=2026,
        month=5,
        line_value=D("64.94"),
        days=reserves + (callout_day,),
    )
    result = compute_pay(lower_month(month))
    assert result.option1_floor == D("65.68")
    assert result.option3_earned == D("65.62")
    assert result.option2_workdays_dpg == D("64.94")
    assert result.base_monthly_pch == D("65.68")
    assert result.winning_option == WinningOption.FLOOR
    assert result.topup_pch == D("0.06")
    _approx(result.total_pay, D("65.68") * D("100"))


# ── 4. Voluntary drop + open time ────────────────────────────────────────
def test_lowering_voluntary_drop_plus_open_time():
    remaining_reserves = tuple(
        Day(
            date=date(2026, 5, d),
            duty_type=DutyType.RSV,
            pch_value=D("3.82"),
            reason_code=ReasonCode.FLOWN,
            workdays=1,
            label=f"RSV-{d}",
        )
        for d in range(1, 15)  # 14 remaining
    )
    dropped_reserves = tuple(
        Day(
            date=date(2026, 5, 14 + i),
            duty_type=DutyType.RSV,
            pch_value=D("3.82"),
            reason_code=ReasonCode.VOLUNTARY_DROP,
            workdays=0,
            label=f"DROP-{i}",
        )
        for i in range(1, 4)  # 3 dropped
    )
    open_time_trip = Trip(
        trip_id="OT-1",
        published_pch=D("17.19"),
        reason_code=ReasonCode.FLOWN,
        premium_category=PremiumCategory.OPEN_TIME_MID_MONTH,
        workdays=1,
    )

    month = Month(
        pilot=_pilot(),
        year=2026,
        month=5,
        line_value=D("65"),
        trips=(open_time_trip,),
        days=remaining_reserves + dropped_reserves,
    )
    result = compute_pay(lower_month(month))
    assert result.option1_floor == D("70.67")
    assert result.option3_earned == D("70.67")
    assert result.base_monthly_pch == D("70.67")
    assert result.topup_pch == D("0.00")
    # 14 reserves @ 1.0× + 17.19 OT @ 1.5×
    _approx(
        result.earned_dollars,
        D("14") * D("3.82") * D("100") + D("17.19") * D("100") * D("1.5"),
    )


# ── Bonus: §3.E.1.b protection via Trip.versions ─────────────────────────
def test_lowering_uses_effective_pch_from_assignment_versions():
    """A trip with revisions pays the high-water mark (max across versions)."""
    from nac_pay.schedule import AssignmentVersion

    # Original 5.33, revised down to 4.00 — pilot is protected at 5.33.
    trip = Trip(
        trip_id="DAY-720",
        published_pch=D("5.33"),
        versions=(AssignmentVersion(seq=1, pch_value=D("4.00")),),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    month = Month(
        pilot=_pilot(),
        year=2026,
        month=5,
        line_value=D("65"),
        trips=(trip,),
    )
    result = compute_pay(lower_month(month))
    # The trip's chunk contributes 5.33 PCH (the protected original), not 4.00.
    trip_chunk = next(c for c in result.per_chunk if c.source_id == "DAY-720")
    assert trip_chunk.raw_pch == D("5.33")
