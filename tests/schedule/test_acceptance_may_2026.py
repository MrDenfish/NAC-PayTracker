"""May 2026 acceptance test — schedule-layer slice.

Companion to ``tests/engine/test_acceptance_may_2026.py``. Same month,
same expected outputs, but expressed as a ``Month`` domain object run
through ``lower_month`` + ``compute_pay``. Proves the end-to-end pipe
(domain model → lowering → engine) reproduces the published acceptance
result.

Modeling notes:

- **FLT 766** uses its real packet data: published 4.17 (May packet p.8,
  trip "766/766/767"), versioned to 5.00 by the May 1 leg-add event.
  This exercises ``Trip.versions`` + §3.E.1.b through lowering.

- **FLT 724** is not in the May packet — its original PCH is unknown,
  so we can't faithfully model it as its own Trip with versions. We
  fold its reassignment uplift (the 2.71 PCH delta needed to bring the
  total Regular pay to 80.29) into the "rest of line" trip rather than
  fabricate an original. The §3.E.1.b duty-extension rule itself is
  covered separately in tests/engine/test_reassignment.py.

- The awarded line of 65.29 PCH is split: 4.17 to FLT 766, 61.12 to the
  rest. The rest carries the 2.71 PCH of 724 uplift on top, total 63.83.

- The 3 added reserves are FLOWN reserve days (involuntary excess over
  DPG = 0; no floor delta beyond Option 3).

- The qualifying open-time pickup is a Trip with premium OPEN_TIME_MID_MONTH
  (1.5×), which lowers to both an OPEN_TIME chunk and an
  OPEN_TIME_PICKUP floor event.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nac_pay.engine import WinningOption, compute_pay
from nac_pay.schedule import (
    AssignmentVersion,
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
RATE = D("124.59")


@pytest.fixture
def may_2026_month() -> Month:
    pilot = PilotProfile(
        pilot_id="MEZ",
        name="Test FO",
        position=Position.FO,
        hourly_rate=RATE,
    )

    flt_766 = Trip(
        trip_id="FLT-766",
        published_pch=D("4.17"),            # May packet p.8
        versions=(
            AssignmentVersion(
                seq=1,
                pch_value=D("5.00"),         # May 1 leg-add event
                label="May 1 leg-add reassignment",
            ),
        ),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        label="FLT 766 (Sun-base pairing, reassigned)",
    )

    # Everything else from the awarded line, plus the 724 reassignment uplift
    # (2.71 PCH) folded in — see module docstring for why we don't break out
    # 724 as its own Trip.
    rest_of_line = Trip(
        trip_id="MAY-LINE-REST",
        published_pch=D("63.83"),            # 65.29 - 4.17 + 2.71
        reason_code=ReasonCode.FLOWN,
        workdays=14,
        label="May awarded line less FLT 766, incl. FLT 724 uplift",
    )

    reserves = tuple(
        Day(
            date=date(2026, 5, d),
            duty_type=DutyType.RSV,
            pch_value=D("3.82"),
            reason_code=ReasonCode.FLOWN,
            workdays=1,
            label=f"Added RES 1021 May {d}",
        )
        for d in (8, 15, 31)
    )

    open_time = Trip(
        trip_id="OT-PREMIUM-MAY",
        published_pch=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        premium_category=PremiumCategory.OPEN_TIME_MID_MONTH,
        workdays=1,
        label="Open-time pickup qualifying for premium",
    )

    return Month(
        pilot=pilot,
        year=2026,
        month=5,
        line_value=D("65.29"),
        trips=(flt_766, rest_of_line, open_time),
        days=reserves,
    )


def test_may_2026_via_lowering_matches_engine_slice(may_2026_month):
    r = compute_pay(lower_month(may_2026_month))

    # Option 1 floor: max(65.29, 65) = 65.29 + 3.82 on-top OT = 69.11.
    assert r.option1_floor == D("69.11")
    # Option 3 earned: 5.00 + 63.83 + 3×3.82 + 3.82 = 84.11.
    assert r.option3_earned == D("84.11")
    # Option 2: 19 workdays × 3.82 = 72.58.
    assert r.option2_workdays_dpg == D("72.58")
    assert r.base_monthly_pch == D("84.11")
    assert r.winning_option == WinningOption.EARNED
    assert r.topup_pch == D("0.00")
    assert r.topup_dollars == D("0.00")


def test_may_2026_via_lowering_category_dollars(may_2026_month):
    r = compute_pay(lower_month(may_2026_month))

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

    expected_regular = D("80.29") * RATE           # 10003.3311 → 10003.33
    expected_open_time = D("3.82") * RATE * D("1.5")  # 713.9007  → 713.90
    expected_total = expected_regular + expected_open_time

    assert r.total_pay == expected_total.quantize(D("0.01"))
    assert r.total_pay == D("10717.23")


def test_flt_766_chunk_uses_effective_pch_from_versions(may_2026_month):
    """The reassignment version (5.00) wins over the published 4.17."""
    r = compute_pay(lower_month(may_2026_month))
    flt_766_chunk = next(c for c in r.per_chunk if c.source_id == "FLT-766")
    assert flt_766_chunk.raw_pch == D("5.00")
    assert flt_766_chunk.multiplier == D("1.0")


def test_open_time_pickup_emits_both_chunk_and_floor_event(may_2026_month):
    """The 1.5× pickup must show up in BOTH the chunk list (earned) and the
    floor (on-top of Option 1). Option 1 = 65.29 + 3.82 = 69.11 proves the
    floor event fired; Option 3 = 84.11 - 80.29 = 3.82 proves the chunk did."""
    r = compute_pay(lower_month(may_2026_month))
    ot_chunk = next(c for c in r.per_chunk if c.source_id == "OT-PREMIUM-MAY")
    assert ot_chunk.raw_pch == D("3.82")
    assert ot_chunk.multiplier == D("1.5")
    # Floor proves the on-top event landed.
    assert r.option1_floor == D("65.29") + D("3.82")
