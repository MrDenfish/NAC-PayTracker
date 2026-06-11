"""Tests for ``schedule.apply_overrides_to_month``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from nac_pay.engine import compute_pay
from nac_pay.schedule import (
    Day,
    DutyType,
    Month,
    PilotProfile,
    Position,
    PremiumCategory,
    ReasonCode,
    Trip,
    apply_overrides_to_month,
    lower_month,
)
from nac_pay.storage import DayOverride


D = Decimal


def _pilot() -> PilotProfile:
    return PilotProfile(
        pilot_id="DFI",
        name="X",
        position=Position.FO,
        hourly_rate=D("124.59"),
    )


def test_no_overrides_returns_unchanged_month():
    month = Month(pilot=_pilot(), year=2026, month=6, line_value=D("65"))
    out = apply_overrides_to_month(month, {})
    assert out is month


def test_override_replaces_trip_reason_code():
    trip = Trip(
        trip_id="768",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 12),),
    )
    month = Month(pilot=_pilot(), year=2026, month=6, line_value=D("65"), trips=(trip,))
    overrides = {"2026-06-12": DayOverride(date_iso="2026-06-12", reason_code="SICK")}
    out = apply_overrides_to_month(month, overrides)
    assert out.trips[0].reason_code is ReasonCode.SICK


def test_override_replaces_day_premium_and_multiplier():
    day = Day(
        date=date(2026, 6, 16),
        duty_type=DutyType.RSV,
        pch_value=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        label="1021",
    )
    month = Month(pilot=_pilot(), year=2026, month=6, line_value=D("65"), days=(day,))
    overrides = {
        "2026-06-16": DayOverride(
            date_iso="2026-06-16",
            premium_category="CUSTOM",
            custom_multiplier="2.5",
        )
    }
    out = apply_overrides_to_month(month, overrides)
    assert out.days[0].premium_category is PremiumCategory.CUSTOM
    assert out.days[0].custom_multiplier == D("2.5")


def test_override_with_invalid_enum_value_is_silently_ignored():
    """Malformed JSON shouldn't crash the pipeline; bad values are skipped."""
    trip = Trip(
        trip_id="768",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 12),),
    )
    month = Month(pilot=_pilot(), year=2026, month=6, line_value=D("65"), trips=(trip,))
    overrides = {
        "2026-06-12": DayOverride(date_iso="2026-06-12", reason_code="BOGUS")
    }
    out = apply_overrides_to_month(month, overrides)
    assert out.trips[0].reason_code is ReasonCode.FLOWN


def test_trip_override_takes_priority_over_day_when_both_share_date():
    trip = Trip(
        trip_id="768",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 12),),
    )
    day = Day(
        date=date(2026, 6, 12),
        duty_type=DutyType.RSV,
        pch_value=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    month = Month(
        pilot=_pilot(), year=2026, month=6, line_value=D("65"),
        trips=(trip,), days=(day,),
    )
    overrides = {"2026-06-12": DayOverride(date_iso="2026-06-12", reason_code="SICK")}
    out = apply_overrides_to_month(month, overrides)
    # The Trip caught it; the Day stays untouched.
    assert out.trips[0].reason_code is ReasonCode.SICK
    assert out.days[0].reason_code is ReasonCode.FLOWN


def test_override_changes_engine_result_end_to_end():
    """Override a FLT day's premium category to OPEN_TIME_MID_MONTH and verify
    the engine bumps total pay by the multiplier delta. Trip PCH 4.17 at 1.0×
    = $519.54; at 1.5× = $779.31; delta = $259.77."""
    trip = Trip(
        trip_id="768",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 12),),
    )
    month = Month(pilot=_pilot(), year=2026, month=6, line_value=D("65"), trips=(trip,))

    before = compute_pay(lower_month(month))
    overrides = {
        "2026-06-12": DayOverride(
            date_iso="2026-06-12",
            premium_category="OPEN_TIME_MID_MONTH",
        )
    }
    after = compute_pay(lower_month(apply_overrides_to_month(month, overrides)))

    # 4.17 × $124.59 × 0.5 = $259.77 of additional earned dollars from the premium
    earned_delta = after.earned_dollars - before.earned_dollars
    assert earned_delta == D("259.77")
