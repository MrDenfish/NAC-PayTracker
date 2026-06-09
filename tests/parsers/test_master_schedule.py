"""Master Schedule PDF parser tests.

Two PDFs in docs/ are the fixtures: May and June 2026 ANC 737 FO Final
Awards. The assertions are deliberately specific (FISHER's day-by-day
grid in May matches what we already verified by hand) so a regression
in pdfplumber's table extraction would surface immediately.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.engine.constants import MPG
from nac_pay.parsers import parse_master_schedule

DOCS = Path(__file__).resolve().parents[2] / "docs"
MAY_PDF = DOCS / "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"
JUN_PDF = DOCS / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf"

D = Decimal


# ── May 2026 ────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def may_pilots():
    return parse_master_schedule(str(MAY_PDF))


def test_may_parses_15_pilots(may_pilots):
    assert len(may_pilots) == 15


def test_may_year_and_month_inferred(may_pilots):
    fisher = may_pilots["DFI"]
    assert fisher.year == 2026
    assert fisher.month == 5


def test_may_fisher_line_value_is_65_29(may_pilots):
    """The May acceptance test target — must match docs/Acceptance_test_May_2026.md."""
    fisher = may_pilots["DFI"]
    assert fisher.last_name == "FISHER"
    assert fisher.line_value == D("65.29")
    assert fisher.monthly_floor == D("65.29")   # already ≥ MPG, no bump


def test_may_fisher_day_pattern_matches_hand_check(may_pilots):
    """May 1 = FLT 766 @ 4.17; 16 reserve days @ 3.82; rest OFF.
    Sum: 4.17 + 16×3.82 = 65.29.
    """
    fisher = may_pilots["DFI"]
    assert len(fisher.days) == 31

    day_1 = next(d for d in fisher.days if d.date == date(2026, 5, 1))
    assert day_1.assignment_id == "766"
    assert day_1.duty_type == "FLT"
    assert day_1.pch_value == D("4.17")

    rsv_days = [d for d in fisher.days if d.duty_type == "RSV"]
    assert len(rsv_days) == 16
    assert all(d.assignment_id == "1021" for d in rsv_days)
    assert all(d.pch_value == D("3.82") for d in rsv_days)

    pch_sum = sum(
        (d.pch_value for d in fisher.days if d.pch_value is not None),
        D("0"),
    )
    assert pch_sum == fisher.line_value


def test_may_acceptance_test_days_8_15_31_are_off(may_pilots):
    """The May acceptance test events add reserves on May 8, 15, 31 — those
    days must be OFF in the original Final Award for the events to make sense."""
    fisher = may_pilots["DFI"]
    for d_no in (8, 15, 31):
        day = next(d for d in fisher.days if d.date == date(2026, 5, d_no))
        assert day.is_off, f"May {d_no} should be OFF in the May Final Award; got {day}"


def test_may_sub_65_lines_get_floored_to_65(may_pilots):
    """HUFFMAN / JOHANSSON / VELASQUEZ all have 17 reserve days × 3.82 = 64.94,
    which the spec says is floored to 65."""
    for code in ("JHU", "RJH", "BE"):
        s = may_pilots[code]
        assert s.line_value == D("64.94"), f"{code} line should be 64.94, got {s.line_value}"
        assert s.monthly_floor == MPG, f"{code} floor should be {MPG}, got {s.monthly_floor}"


def test_may_cully_is_fmla_entire_month(may_pilots):
    """DCU/CULLY is FMLA all 31 days — line value 0, floor still 65."""
    cully = may_pilots["DCU"]
    assert cully.last_name == "CULLY"
    assert cully.line_value == D("0") or cully.line_value == D("0.00")
    assert cully.monthly_floor == MPG
    fmla_days = [d for d in cully.days if d.duty_type == "FMLA"]
    assert len(fmla_days) == 31


def test_may_flight_pilots_have_trip_pairings(may_pilots):
    """KWR/WRIGHT is a flying line — assignment IDs should contain trip
    pairings (e.g. ``720/R1``, ``720/750``), not just leave labels."""
    wright = may_pilots["KWR"]
    flt_days = [d for d in wright.days if d.duty_type == "FLT"]
    assert len(flt_days) > 10
    # Some pairings have '/' in them (multi-leg trip identifiers)
    assert any("/" in (d.assignment_id or "") for d in flt_days)


# ── June 2026 ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def jun_pilots():
    return parse_master_schedule(str(JUN_PDF))


def test_june_parses_15_pilots(jun_pilots):
    assert len(jun_pilots) == 15


def test_june_year_month_and_30_days(jun_pilots):
    fisher = jun_pilots["DFI"]
    assert fisher.year == 2026
    assert fisher.month == 6
    assert len(fisher.days) == 30
    # First day of June and last day of June bracketing the grid.
    assert fisher.days[0].date == date(2026, 6, 1)
    assert fisher.days[-1].date == date(2026, 6, 30)


def test_june_fisher_line_value(jun_pilots):
    """Sanity check — the June parse should produce a non-zero line value
    that matches the printed total. We don't pin the exact value (since
    it's not the acceptance target), just that it's plausible."""
    fisher = jun_pilots["DFI"]
    assert fisher.line_value > 0
    pch_sum = sum(
        (d.pch_value for d in fisher.days if d.pch_value is not None),
        D("0"),
    )
    assert pch_sum == fisher.line_value
