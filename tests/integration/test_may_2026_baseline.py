"""End-to-end baseline: May 2026 FA PDF → Month → engine → pay.

This is the **awarded-line baseline** for FISHER (the May acceptance
test target): she flies her line as published, no mid-month events.
Earned should equal her line value (65.29 PCH), floor = 65.29, top-up
zero, total pay = 65.29 × $124.59.

Proves the full pipe composes against real-world input:
    PDF → parse_master_schedule → month_from_master_schedule → lower_month
        → compute_pay
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.engine import WinningOption, compute_pay
from nac_pay.parsers import parse_master_schedule
from nac_pay.schedule import (
    DutyType,
    PilotProfile,
    Position,
    ReasonCode,
    lower_month,
    month_from_master_schedule,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
MAY_PDF = DOCS / "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"

D = Decimal


@pytest.fixture(scope="module")
def fisher_may_schedule():
    return parse_master_schedule(str(MAY_PDF))["DFI"]


@pytest.fixture
def fisher_profile():
    return PilotProfile(
        pilot_id="DFI",
        name="FISHER",
        position=Position.FO,
        hourly_rate=D("124.59"),
    )


def test_baseline_conversion_emits_expected_trip_and_day_count(
    fisher_may_schedule, fisher_profile
):
    month, warnings = month_from_master_schedule(fisher_may_schedule, fisher_profile)
    assert warnings == ()
    assert month.year == 2026
    assert month.month == 5
    assert month.line_value == D("65.29")
    # 1 FLT day (May 1 FLT 766) → 1 Trip; 16 RSV days → 16 Days; 14 OFF → skipped.
    assert len(month.trips) == 1
    assert len(month.days) == 16


def test_baseline_trip_carries_real_packet_pch_for_flt_766(
    fisher_may_schedule, fisher_profile
):
    month, _ = month_from_master_schedule(fisher_may_schedule, fisher_profile)
    trip = month.trips[0]
    assert trip.trip_id == "766"
    assert trip.published_pch == D("4.17")
    assert trip.reason_code is ReasonCode.FLOWN
    assert trip.workdays == 1


def test_baseline_reserve_days_default_to_flown_at_dpg(
    fisher_may_schedule, fisher_profile
):
    month, _ = month_from_master_schedule(fisher_may_schedule, fisher_profile)
    assert all(d.duty_type is DutyType.RSV for d in month.days)
    assert all(d.reason_code is ReasonCode.FLOWN for d in month.days)
    assert all(d.pch_value == D("3.82") for d in month.days)
    assert all(d.workdays == 1 for d in month.days)


def test_baseline_pay_matches_line_value_no_topup(
    fisher_may_schedule, fisher_profile
):
    """The point of the integration: FISHER's awarded line is 65.29 PCH.
    With no mid-month events, that's exactly what she earns — no top-up,
    no premium. Total pay = 65.29 × $124.59."""
    month, _ = month_from_master_schedule(fisher_may_schedule, fisher_profile)
    result = compute_pay(lower_month(month))

    # Earned = 4.17 + 16 × 3.82 = 65.29
    assert result.option3_earned == D("65.29")
    # Floor = max(65.29, 65) = 65.29
    assert result.option1_floor == D("65.29")
    # Workdays × DPG = 17 × 3.82 = 64.94 — loses
    assert result.option2_workdays_dpg == D("64.94")
    # Earned exactly meets the floor; no top-up
    assert result.base_monthly_pch == D("65.29")
    assert result.winning_option in (WinningOption.EARNED, WinningOption.FLOOR)
    assert result.topup_pch == D("0.00")
    assert result.topup_dollars == D("0.00")

    expected = D("65.29") * D("124.59")    # 8134.4811 → 8134.48
    assert result.total_pay == expected.quantize(D("0.01"))
    assert result.total_pay == D("8134.48")


def test_baseline_pipeline_runs_for_all_15_may_pilots(fisher_profile):
    """Smoke: every parsed pilot in May should lower + price without raising."""
    pilots = parse_master_schedule(str(MAY_PDF))
    for code, sched in pilots.items():
        # Reuse one profile (rate matters; identity doesn't for this smoke).
        month, _warnings = month_from_master_schedule(sched, fisher_profile)
        result = compute_pay(lower_month(month))
        assert result.total_pay >= D("0"), f"{code} produced negative pay"
