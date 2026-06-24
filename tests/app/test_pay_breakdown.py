"""Pay breakdown view tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import (
    PayBreakdownData,
    _build_earning_rows,
    _categorize,
    _pipeline,
    load_pay_breakdown,
)
from nac_pay.engine import ChunkKind, ChunkResult, compute_pay
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


client = TestClient(app)
D = Decimal


# ── Route ──────────────────────────────────────────────────────────────


def test_pay_route_renders_june():
    r = client.get("/pay?ym=2026-6")
    assert r.status_code == 200
    assert "Pay breakdown" in r.text
    assert "June 2026" in r.text
    assert "Regular Pay" in r.text
    assert "$8,195.53" in r.text or "$8,195.42" in r.text


def test_pay_route_renders_may():
    r = client.get("/pay?ym=2026-5")
    assert r.status_code == 200
    assert "May 2026" in r.text
    assert "65.29" in r.text
    assert "$8,134.48" in r.text


def test_pay_route_active_nav():
    r = client.get("/pay?ym=2026-6")
    assert 'href="/pay?ym=2026-6" class="nav-link nav-link--active"' in r.text


def test_pay_route_invalid_ym():
    assert client.get("/pay?ym=garbage").status_code == 400


def test_pay_route_unknown_month():
    assert client.get("/pay?year=2030&month=1").status_code == 404


# ── Categorization unit tests ──────────────────────────────────────────


def _chunk(kind: ChunkKind, mult: str = "1.0") -> ChunkResult:
    return ChunkResult(
        source_id="x", kind=kind, raw_pch=D("1"),
        multiplier=D(mult), rate=D("100"), dollars=D("100"),
    )


def test_categorize_special_kinds_get_their_own_category():
    assert _categorize(_chunk(ChunkKind.PTO)) == "Paid Time Off"
    assert _categorize(_chunk(ChunkKind.SICK)) == "Sick"
    assert _categorize(_chunk(ChunkKind.JURY)) == "Jury Duty"
    assert _categorize(_chunk(ChunkKind.BEREAVEMENT)) == "Bereavement"
    assert _categorize(_chunk(ChunkKind.HOME_STUDY)) == "Home Study"
    assert _categorize(_chunk(ChunkKind.TRAINING)) == "Training"
    assert _categorize(_chunk(ChunkKind.MOVING)) == "Moving"


def test_categorize_open_time_at_premium_is_open_time():
    assert _categorize(_chunk(ChunkKind.OPEN_TIME, "1.5")) == "Open Time"


def test_categorize_open_time_at_regular_rolls_into_regular_pay():
    """Open time picked up during bid period (P.1) pays at 1.0× — per the
    user's terminology note it rolls into Regular Pay on the stub."""
    assert _categorize(_chunk(ChunkKind.OPEN_TIME, "1.0")) == "Regular Pay"


def test_categorize_trips_reserves_default_to_regular_pay():
    assert _categorize(_chunk(ChunkKind.TRIP)) == "Regular Pay"
    assert _categorize(_chunk(ChunkKind.RESERVE_DAY)) == "Regular Pay"
    assert _categorize(_chunk(ChunkKind.MILITARY)) == "Regular Pay"
    assert _categorize(_chunk(ChunkKind.OTHER)) == "Regular Pay"


# ── Row aggregation ─────────────────────────────────────────────────────


def test_build_rows_merges_same_category_multiplier():
    chunks = (
        ChunkResult("trip-1", ChunkKind.TRIP, D("4.17"), D("1.0"), D("124.59"), D("519.54")),
        ChunkResult("rsv-1", ChunkKind.RESERVE_DAY, D("3.82"), D("1.0"), D("124.59"), D("475.93")),
        ChunkResult("rsv-2", ChunkKind.RESERVE_DAY, D("3.82"), D("1.0"), D("124.59"), D("475.93")),
    )
    rows = _build_earning_rows(chunks, base_rate=D("124.59"))
    assert len(rows) == 1
    row = rows[0]
    assert row.pay_type == "Regular Pay"
    assert row.pch == D("11.81")
    # 11.81 × 124.59 = 1471.4079 → $1,471.41
    assert row.amount == D("1471.41")


def test_build_rows_splits_distinct_multipliers():
    chunks = (
        ChunkResult("trip", ChunkKind.TRIP, D("80.29"), D("1.0"), D("124.59"), D("10003.33")),
        ChunkResult("ot", ChunkKind.OPEN_TIME, D("3.82"), D("1.5"), D("186.885"), D("713.90")),
    )
    rows = _build_earning_rows(chunks, base_rate=D("124.59"))
    assert len(rows) == 2
    regular = next(r for r in rows if r.pay_type == "Regular Pay")
    open_time = next(r for r in rows if r.pay_type == "Open Time")
    assert regular.pch == D("80.29")
    assert regular.multiplier == D("1.0")
    assert open_time.pch == D("3.82")
    assert open_time.multiplier == D("1.5")
    # 3.82 × (124.59 × 1.5) = 3.82 × 186.885 = 713.9007 → $713.90
    assert open_time.amount == D("713.90")
    # Sort order: Regular Pay before Open Time
    assert [r.pay_type for r in rows] == ["Regular Pay", "Open Time"]


# ── Loader content ─────────────────────────────────────────────────────


def test_load_pay_breakdown_baseline_june_single_regular_row():
    """June FISHER with no events: single Regular Pay row, no top-up, all
    earnings at $124.59 regular rate."""
    d = load_pay_breakdown(2026, 6)
    assert len(d.earning_rows) == 1
    row = d.earning_rows[0]
    assert row.pay_type == "Regular Pay"
    assert row.pch == D("65.78")
    assert row.multiplier == D("1.0")
    assert row.rate == D("124.590")
    assert row.amount == D("8195.53")
    assert d.topup_pch == D("0.00")
    assert d.total_pay == D("8195.53")
    assert d.winning_key == "floor"
    assert d.option1_floor == D("65.78")


def test_load_pay_breakdown_may_baseline_matches_dashboard():
    """May FISHER: 65.29 PCH × $124.59 = $8,134.4811 → $8,134.48,
    matching the May acceptance test baseline."""
    d = load_pay_breakdown(2026, 5)
    assert d.earning_rows[0].amount == D("8134.48")
    assert d.total_pay == D("8134.48")


# ── Synthetic premium pickup via pipeline-poke ────────────────────────


def test_synthetic_open_time_pickup_produces_two_earning_rows():
    """Construct a synthetic Month that mirrors the May 2026 acceptance
    scenario (80.29 Regular + 3.82 Open Time premium), run the engine,
    and assert the breakdown has both rows with the right amounts."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)

    pilot = real.pilot
    rate = pilot.hourly_rate
    # Build a Month with mixed multipliers via Trip.premium_category.
    line_trip = Trip(
        trip_id="LINE",
        published_pch=D("80.29"),
        reason_code=ReasonCode.FLOWN,
        workdays=15,
        label="line",
    )
    ot_trip = Trip(
        trip_id="OT-PREMIUM",
        published_pch=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        premium_category=PremiumCategory.OPEN_TIME_MID_MONTH,
        workdays=1,
        label="open-time premium",
    )
    poked_month = Month(
        pilot=pilot,
        year=2026,
        month=6,
        line_value=D("65.29"),
        trips=(line_trip, ot_trip),
    )
    poked_result = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=poked_month,
        engine_result=compute_pay(lower_month(poked_month)),
        applied_events=(),
        validation_discrepancies=(),
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked_result):
        d = load_pay_breakdown(2026, 6)

    pay_types = [r.pay_type for r in d.earning_rows]
    assert "Regular Pay" in pay_types
    assert "Open Time" in pay_types

    regular = next(r for r in d.earning_rows if r.pay_type == "Regular Pay")
    open_time = next(r for r in d.earning_rows if r.pay_type == "Open Time")

    assert regular.pch == D("80.29")
    assert regular.amount == D("10003.33")
    assert open_time.pch == D("3.82")
    assert open_time.multiplier == D("1.5")
    # 3.82 × 124.59 × 1.5 = 713.9007 → $713.90
    assert open_time.amount == D("713.90")
    # Total = 80.29 × 124.59 + 3.82 × 124.59 × 1.5 = 10003.33 + 713.90 = 10,717.23
    assert d.total_pay == D("10717.23")


def test_synthetic_topup_appears_when_earned_below_floor():
    """A month where Option 1 floor > Option 3 earned forces a top-up row
    paid at the regular rate. Synthesize the light-protected-month worked
    check (§6): line 50 → floor 65; flew 40 + sick 10; earned 50; top-up 15."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    pilot = real.pilot

    flown_trip = Trip(
        trip_id="FLOWN",
        published_pch=D("40"),
        reason_code=ReasonCode.FLOWN,
        workdays=10,
    )
    sick_day = Day(
        date=None,
        duty_type=DutyType.FLT,
        pch_value=D("10"),
        reason_code=ReasonCode.SICK,
        workdays=3,
    )
    poked_month = Month(
        pilot=pilot,
        year=2026,
        month=6,
        line_value=D("50"),
        trips=(flown_trip,),
        days=(sick_day,),
    )
    poked_result = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=poked_month,
        engine_result=compute_pay(lower_month(poked_month)),
        applied_events=(),
        validation_discrepancies=(),
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked_result):
        d = load_pay_breakdown(2026, 6)

    assert d.topup_pch == D("15.00")
    # 15 × 124.59 = $1,868.85
    assert d.topup_dollars == D("1868.85")
    # Earned should be 50 PCH = 40 Regular + 10 Sick
    earned = sum(r.pch for r in d.earning_rows)
    assert earned == D("50")
    # Total = 50 × 124.59 + 15 × 124.59 = 65 × 124.59 = $8,098.35
    assert d.total_pay == D("8098.35")
    assert d.winning_key == "floor"
