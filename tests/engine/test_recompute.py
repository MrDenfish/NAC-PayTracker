"""§3.E recompute helper for Detailed-mode reassignment entry (Phase G)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.engine import recompute_pch_from_times


def _D(x: str | int) -> Decimal:
    return Decimal(str(x))


def test_flight_op_wins_when_block_is_largest():
    # block=6 → flight_op=6, duty/2=5, tafb/4.90≈4.08, dpg=3.82 → max=6
    pch = recompute_pch_from_times(
        block_hours=_D("6.00"), duty_hours=_D("10.00"),
        tafb_hours=_D("20.00"), workdays=1,
    )
    assert pch == Decimal("6.00")


def test_duty_rig_wins_when_long_duty():
    # block=4, duty=14 → duty/2=7 (wins), tafb=15/4.90≈3.06, dpg=3.82
    pch = recompute_pch_from_times(
        block_hours=_D("4.00"), duty_hours=_D("14.00"),
        tafb_hours=_D("15.00"), workdays=1,
    )
    assert pch == Decimal("7.00")


def test_trip_rig_wins_when_long_tafb():
    # block=3, duty=8, tafb=49 → tafb/4.90=10.0 (wins), duty/2=4, dpg=3.82
    pch = recompute_pch_from_times(
        block_hours=_D("3.00"), duty_hours=_D("8.00"),
        tafb_hours=_D("49.00"), workdays=1,
    )
    assert pch == Decimal("10")


def test_cumulative_dpg_wins_for_short_trip_multi_workday():
    # block=2, duty=4, tafb=8, workdays=3 → dpg=11.46 (wins)
    pch = recompute_pch_from_times(
        block_hours=_D("2.00"), duty_hours=_D("4.00"),
        tafb_hours=_D("8.00"), workdays=3,
    )
    assert pch == Decimal("11.46")


def test_deadhead_added_on_top_of_winning_component():
    # block=5 wins; deadhead 0.50 is additive
    pch = recompute_pch_from_times(
        block_hours=_D("5.00"), duty_hours=_D("8.00"),
        tafb_hours=_D("15.00"), workdays=1,
        deadhead=_D("0.50"),
    )
    assert pch == Decimal("5.50")


def test_default_workdays_is_1():
    # Single-day reassignment — workdays defaults to 1, DPG = 3.82
    pch = recompute_pch_from_times(
        block_hours=_D("3.00"), duty_hours=_D("6.00"),
        tafb_hours=_D("10.00"),
    )
    # duty/2 = 3.0, block=3.0, tafb/4.90≈2.04, dpg=3.82 (wins)
    assert pch == Decimal("3.82")


def test_duty_extension_scenario_from_spec():
    """Spec §3.E example: original PCH 5, duty extends 8→12 hrs.
    Recomputed PCH = max(block, 12/2=6, ...) = 6.
    The reassignment greater-of (5 vs 6) is applied elsewhere; this
    helper just produces the recompute."""
    # Use block=5 (matches original) so the duty-rig path wins
    pch = recompute_pch_from_times(
        block_hours=_D("5.00"), duty_hours=_D("12.00"),
        tafb_hours=_D("12.00"), workdays=1,
    )
    assert pch == Decimal("6")
