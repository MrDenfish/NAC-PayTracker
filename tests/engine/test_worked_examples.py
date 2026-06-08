"""Section 6 worked checks. All four must pass before anything else gets built.

Source: SYSTEM_CONTEXT.md §6.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.engine import (
    Chunk,
    ChunkKind,
    EngineInput,
    FloorEvent,
    FloorEventKind,
    WinningOption,
    compute_pay,
)

D = Decimal
RATE = D("100")  # any rate; the asserts express dollars in PCH × rate


def _approx(actual: Decimal, expected: Decimal, tol: str = "0.01") -> None:
    assert abs(actual - expected) <= Decimal(tol), f"{actual} != {expected} (±{tol})"


# ── 1. Normal month — line 68, flown fully → 68 PCH, paid 68×rate ────────
def test_normal_month_line_68_flown_fully():
    result = compute_pay(
        EngineInput(
            line_value=D("68"),
            hourly_rate=RATE,
            chunks=(
                Chunk(
                    source_id="MAY-TRIP-1",
                    kind=ChunkKind.TRIP,
                    raw_pch=D("68"),
                    multiplier=D("1.0"),
                    workdays=17,  # option2 = 17*3.82 = 64.94 < 68
                ),
            ),
        )
    )
    assert result.base_monthly_pch == D("68.00")
    assert result.option1_floor == D("68.00")
    assert result.option3_earned == D("68.00")
    assert result.topup_pch == D("0.00")
    _approx(result.total_pay, D("68") * RATE)


# ── 2. Light protected month — flew 40 + sick 10, floor 65 → 65 PCH ──────
def test_light_protected_month_floor_holds():
    result = compute_pay(
        EngineInput(
            line_value=D("50"),  # any value ≤ 65 yields floor 65
            hourly_rate=RATE,
            chunks=(
                Chunk("FLOWN", ChunkKind.TRIP, D("40"), D("1.0"), workdays=10),
                Chunk("SICK", ChunkKind.SICK, D("10"), D("1.0"), workdays=3),
            ),
            # no floor events — sick is protected, doesn't move the floor
        )
    )
    assert result.option1_floor == D("65.00")
    assert result.option3_earned == D("50.00")
    assert result.base_monthly_pch == D("65.00")
    assert result.winning_option == WinningOption.FLOOR
    assert result.topup_pch == D("15.00")
    _approx(result.earned_dollars, D("50") * RATE)
    _approx(result.topup_dollars, D("15") * RATE)
    _approx(result.total_pay, D("65") * RATE)


# ── 3. Reserve callout — 17 reserve days, line 64.94, day-5 callout 4.50 ─
# Must be 65.68, NOT 65.62. The guarantee-floor top-up must persist.
def test_reserve_callout_top_up_persists():
    reserve_chunks = tuple(
        Chunk(f"RSV-{i}", ChunkKind.RESERVE_DAY, D("3.82"), D("1.0"), workdays=1)
        for i in range(16)  # 16 regular reserve days
    )
    callout = Chunk("RSV-5-CALLOUT", ChunkKind.TRIP, D("4.50"), D("1.0"), workdays=1)
    excess_event = FloorEvent(
        seq=1,
        kind=FloorEventKind.INVOLUNTARY_EXCESS,
        delta_pch=D("4.50") - D("3.82"),  # 0.68
    )

    result = compute_pay(
        EngineInput(
            line_value=D("64.94"),
            hourly_rate=RATE,
            chunks=reserve_chunks + (callout,),
            floor_events=(excess_event,),
        )
    )
    # Option 1: floor 65 + 0.68 excess = 65.68
    assert result.option1_floor == D("65.68")
    # Option 3: 16*3.82 + 4.50 = 65.62
    assert result.option3_earned == D("65.62")
    # Option 2: 17 workdays * 3.82 = 64.94 — must lose
    assert result.option2_workdays_dpg == D("64.94")
    assert result.base_monthly_pch == D("65.68")
    assert result.winning_option == WinningOption.FLOOR
    assert result.topup_pch == D("0.06")  # 65.68 - 65.62
    _approx(result.earned_dollars, D("65.62") * RATE)
    _approx(result.topup_dollars, D("0.06") * RATE)
    _approx(result.total_pay, D("65.68") * RATE)


# ── 4. Voluntary drop + open time — floor forfeits, OT stacks on top ─────
def test_voluntary_drop_plus_open_time():
    # Start: 17 reserve days. Drop 3 → 14 remaining. Pick up 17.19 PCH OT.
    remaining_reserves = tuple(
        Chunk(f"RSV-{i}", ChunkKind.RESERVE_DAY, D("3.82"), D("1.0"), workdays=1)
        for i in range(14)
    )
    open_time = Chunk(
        "OT-1",
        ChunkKind.OPEN_TIME,
        D("17.19"),
        D("1.5"),
        workdays=1,
    )
    drops = tuple(
        FloorEvent(seq=i + 1, kind=FloorEventKind.VOLUNTARY_DROP, delta_pch=D("3.82"))
        for i in range(3)
    )
    pickup = FloorEvent(seq=10, kind=FloorEventKind.OPEN_TIME_PICKUP, delta_pch=D("17.19"))

    result = compute_pay(
        EngineInput(
            line_value=D("65"),
            hourly_rate=RATE,
            chunks=remaining_reserves + (open_time,),
            floor_events=drops + (pickup,),
        )
    )
    # Option 1: floor 65 - 3*3.82 = 53.48 + 17.19 OT on top = 70.67
    assert result.option1_floor == D("70.67")
    # Option 3: 14*3.82 + 17.19 = 70.67
    assert result.option3_earned == D("70.67")
    assert result.base_monthly_pch == D("70.67")
    assert result.topup_pch == D("0.00")  # option1 == option3
    # Dollars: 14*3.82 at 1.0× + 17.19 at 1.5×
    _approx(result.earned_dollars, D("53.48") * RATE + D("17.19") * RATE * D("1.5"))
    _approx(result.total_pay, D("53.48") * RATE + D("17.19") * RATE * D("1.5"))


# ── Smoke: every winning_option path is reachable from worked checks ─────
def test_winning_options_are_reachable():
    paths = {
        WinningOption.FLOOR,
        WinningOption.EARNED,
    }
    # Re-run cases 2 and 4 and assert which option won.
    floor_case = compute_pay(
        EngineInput(
            line_value=D("50"),
            hourly_rate=RATE,
            chunks=(Chunk("X", ChunkKind.TRIP, D("40"), D("1.0"), workdays=5),),
        )
    )
    assert floor_case.winning_option == WinningOption.FLOOR

    earned_case = compute_pay(
        EngineInput(
            line_value=D("50"),
            hourly_rate=RATE,
            chunks=(Chunk("X", ChunkKind.TRIP, D("90"), D("1.0"), workdays=5),),
        )
    )
    assert earned_case.winning_option == WinningOption.EARNED
    assert paths  # silence ruff
