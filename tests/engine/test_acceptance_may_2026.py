"""May 2026 acceptance test — engine-only slice.

Source: docs/Acceptance_test_May_2026.md (the corrected, terminology-aligned
version). The pilot flew the awarded line plus a few mid-month adds; this
test pins the engine's output for the resulting chunks/floor-events.

This is the *month rollup* check. The reassignment-greater-of (3.E.1.b) is
verified separately by reading original 766/724 PCH from the May packet
once the packet parser exists.
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
RATE = D("124.59")


@pytest.fixture
def may_2026_input() -> EngineInput:
    # All 1.0× chunks roll into "Regular pay" on the stub.
    # Reassignment-delta total is derived from the published category sums
    # in Acceptance_test_May_2026.md: 80.29 Regular - 65.29 line - 11.46
    # reserves = 3.54 of reassignment uplift (FLT 766 + FLT 724 combined).
    line_as_awarded = Chunk(
        source_id="MAY-LINE",
        kind=ChunkKind.TRIP,
        raw_pch=D("65.29"),
        multiplier=D("1.0"),
        workdays=15,
        label="May line as awarded",
    )
    reassignment_uplift = Chunk(
        source_id="REASSIGN-766+724",
        kind=ChunkKind.TRIP,
        raw_pch=D("3.54"),
        multiplier=D("1.0"),
        label="FLT 766 leg-add + FLT 724 duty-rig uplift (combined)",
    )
    reserves = tuple(
        Chunk(
            source_id=f"RES-1021-MAY-{day}",
            kind=ChunkKind.RESERVE_DAY,
            raw_pch=D("3.82"),
            multiplier=D("1.0"),
            workdays=1,
            label=f"Added reserve RES 1021 May {day}",
        )
        for day in (8, 15, 31)
    )
    open_time_premium = Chunk(
        source_id="OT-PREMIUM-MAY",
        kind=ChunkKind.OPEN_TIME,
        raw_pch=D("3.82"),
        multiplier=D("1.5"),
        workdays=1,
        label="Open-time pickup qualifying for premium",
    )
    # Only the qualifying-premium pickup sits "on top" of the floor.
    # The 3 added reserves are involuntary at DPG → excess = 0 → no floor delta.
    # The reassignments don't move the floor either — they just bump trip PCH.
    ot_event = FloorEvent(
        seq=1,
        kind=FloorEventKind.OPEN_TIME_PICKUP,
        delta_pch=D("3.82"),
        label="Premium open-time pickup",
    )
    return EngineInput(
        line_value=D("65.29"),
        hourly_rate=RATE,
        chunks=(line_as_awarded, reassignment_uplift, *reserves, open_time_premium),
        floor_events=(ot_event,),
    )


def test_may_2026_monthly_pch_and_winning_option(may_2026_input):
    r = compute_pay(may_2026_input)
    # Option 1 floor: max(65.29, 65) = 65.29 + 3.82 on-top = 69.11.
    assert r.option1_floor == D("69.11")
    # Option 3 earned: 65.29 + 3.54 + 3 × 3.82 + 3.82 = 84.11.
    assert r.option3_earned == D("84.11")
    # Earned wins (workdays × DPG = 19 × 3.82 = 72.58, also less).
    assert r.base_monthly_pch == D("84.11")
    assert r.winning_option == WinningOption.EARNED
    assert r.topup_pch == D("0.00")
    assert r.topup_dollars == D("0.00")


def test_may_2026_category_dollars(may_2026_input):
    r = compute_pay(may_2026_input)
    # Regular-pay total = sum of all 1.0× chunks at $124.59 × 80.29 PCH.
    regular_pch = sum(
        (c.raw_pch for c in r.per_chunk if c.multiplier == D("1.0")),
        D("0"),
    )
    open_time_pch = sum(
        (c.raw_pch for c in r.per_chunk if c.multiplier == D("1.5")),
        D("0"),
    )
    assert regular_pch == D("80.29")
    assert open_time_pch == D("3.82")

    expected_regular = D("80.29") * RATE                       # 10003.3311 → 10003.33
    expected_open_time = D("3.82") * RATE * D("1.5")           # 713.9007  → 713.90
    expected_total = expected_regular + expected_open_time     # 10717.2318 → 10717.23

    assert r.earned_dollars == expected_total.quantize(D("0.01"))
    assert r.total_pay == expected_total.quantize(D("0.01"))
    # Spot-check stub-format expectations.
    assert r.total_pay == D("10717.23")
