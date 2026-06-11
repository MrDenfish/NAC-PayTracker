"""Pay stub parser tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.parsers import parse_pay_stub

D = Decimal
DOCS = Path(__file__).resolve().parents[2] / "docs"
STUB_DIR = DOCS / "pay Stubs"
MAY_STUB_1 = STUB_DIR / "May_ Base_payStub.pdf"
MAY_STUB_2 = STUB_DIR / "May_payStub.pdf"


@pytest.fixture(scope="module")
def stub1():
    return parse_pay_stub(MAY_STUB_1)


@pytest.fixture(scope="module")
def stub2():
    return parse_pay_stub(MAY_STUB_2)


# ── Header fields ─────────────────────────────────────────────────────


def test_stub1_period_and_pay_date(stub1):
    assert stub1.period_start == date(2026, 5, 1)
    assert stub1.period_end == date(2026, 5, 15)
    assert stub1.pay_date == date(2026, 5, 22)
    assert stub1.net_pay == D("2061.44")


def test_stub2_period_and_pay_date(stub2):
    assert stub2.period_start == date(2026, 5, 16)
    assert stub2.period_end == date(2026, 5, 31)
    assert stub2.pay_date == date(2026, 6, 5)
    assert stub2.net_pay == D("3355.02")


# ── Earnings rows ─────────────────────────────────────────────────────


def test_stub1_is_pure_mpg_advance(stub1):
    """Stub 1 = +32.50 PCH MPG advance at regular rate (= 65/2)."""
    regular = [line for line in stub1.earnings if line.pay_type == "Regular Pay"]
    assert len(regular) == 1
    assert regular[0].hours == D("32.500000")
    assert regular[0].rate == D("124.5900")
    assert regular[0].current_amount == D("4049.18")
    open_time = [line for line in stub1.earnings if line.pay_type == "Open Time"]
    assert open_time[0].hours == D("0.000000")
    assert open_time[0].current_amount == D("0.00")


def test_stub2_has_both_positive_and_negative_regular_rows(stub2):
    """The May acceptance scenario realized: +80.29 actual flying and
    -32.50 advance reversal split across two Regular Pay rows."""
    regular = [line for line in stub2.earnings if line.pay_type == "Regular Pay"]
    assert len(regular) == 2
    hours_set = {line.hours for line in regular}
    assert D("80.290000") in hours_set
    assert D("-32.500000") in hours_set
    # Current amounts: +10,003.33 and -4,049.18
    amounts_set = {line.current_amount for line in regular}
    assert D("10003.33") in amounts_set
    assert D("-4049.18") in amounts_set


def test_stub2_open_time_at_premium_rate(stub2):
    """Open Time on stub 2 = 3.82 hrs × $186.885 = $713.90 (the 1.5× premium
    rate matches the May 2026 acceptance test)."""
    ot = next(line for line in stub2.earnings if line.pay_type == "Open Time")
    assert ot.hours == D("3.820000")
    assert ot.rate == D("186.8850")
    assert ot.current_amount == D("713.90")
    # Premium rate = base × 1.5 = 124.59 × 1.5 = 186.885
    assert ot.rate == D("124.5900") * D("1.5")


def test_group_term_life_benefit_row_has_no_hours_or_rate(stub1):
    """The benefit row format: just Pay Type, Current, YTD — no Hours or Rate."""
    gtl = next(line for line in stub1.earnings if line.pay_type == "Group Term Life")
    assert gtl.hours is None
    assert gtl.rate is None
    assert gtl.current_amount == D("15.84")
    assert gtl.ytd_amount == D("158.40")


def test_total_hours_match_stub_format(stub2):
    """Stub 2: Total Hours Worked 47.79 (= 80.29 - 32.50) and Total Hours
    51.61 (= 47.79 + 3.82 open time premium)."""
    assert stub2.total_hours_worked == D("47.79")
    assert stub2.total_hours == D("51.61")
