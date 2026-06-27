"""Calendar view tests — route + loader + flag flow."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import _pipeline, load_calendar
from nac_pay.engine import compute_pay
from nac_pay.parsers import MatchStatus, ReconciledTrip, ReconciliationResult, TripPairing
from nac_pay.schedule import (
    AssignmentVersion,
    Day,
    DutyType,
    Month,
    PilotProfile,
    Position,
    ReasonCode,
    Trip,
    apply_actuals_to_month,
    lower_month,
    month_from_master_schedule,
)


client = TestClient(app)


# ── Route ──────────────────────────────────────────────────────────────


def test_calendar_route_default_renders():
    r = client.get("/calendar")
    assert r.status_code == 200
    assert "Calendar" in r.text
    assert "Dennis FISHER" in r.text


def test_calendar_route_switcher_may():
    r = client.get("/calendar?ym=2026-5")
    assert r.status_code == 200
    assert "May 2026" in r.text
    # May FLT 766 4.17 should appear
    assert "766" in r.text
    assert "4.17" in r.text


def test_calendar_route_switcher_june():
    r = client.get("/calendar?ym=2026-6")
    assert r.status_code == 200
    assert "June 2026" in r.text
    assert "65.78" in r.text


def test_calendar_route_invalid_ym_400():
    assert client.get("/calendar?ym=oops").status_code == 400


def test_calendar_route_unknown_month_404():
    assert client.get("/calendar?year=2030&month=1").status_code == 404


def test_calendar_link_is_active_in_nav():
    r = client.get("/calendar?ym=2026-6")
    # Nav has both Dashboard and Calendar; Calendar should carry --active.
    assert 'href="/calendar?ym=2026-6" class="nav-link nav-link--active"' in r.text
    assert 'href="/?ym=2026-6" class="nav-link "' in r.text  # dashboard not active


# ── Loader: shape & content ────────────────────────────────────────────


def test_calendar_june_has_five_weeks_seven_days():
    data = load_calendar(2026, 6)
    assert len(data.weeks) == 5  # June 2026: Mon Jun 1 → Sun Jul 5 fits 5 weeks
    assert all(len(week) == 7 for week in data.weeks)


def test_calendar_june_in_month_cell_count_is_30():
    data = load_calendar(2026, 6)
    in_month = [c for week in data.weeks for c in week if c.in_month]
    assert len(in_month) == 30


def test_calendar_june_renders_fishers_flt_days():
    """FISHER's June FA has 7 FLT days. Each shows on the right date with
    the right aid and PCH."""
    data = load_calendar(2026, 6)
    flt_by_date = {
        c.date: c
        for week in data.weeks
        for c in week
        if c.in_month and c.duty_class == "flt"
    }
    expected = {
        date(2026, 6, 1):  ("720/772",  Decimal("5.33")),
        date(2026, 6, 2):  ("722/750",  Decimal("4.92")),
        date(2026, 6, 4):  ("722/R1",   Decimal("5.38")),
        date(2026, 6, 5):  ("722/750",  Decimal("4.92")),
        date(2026, 6, 6):  ("722/754",  Decimal("5.25")),
        date(2026, 6, 12): ("768",      Decimal("4.17")),
        date(2026, 6, 17): ("722/754",  Decimal("5.25")),
    }
    for dt, (aid, pch) in expected.items():
        cell = flt_by_date[dt]
        assert cell.assignment_id == aid
        assert cell.pch == pch


def test_calendar_june_reserve_days_show_rsv_at_dpg():
    data = load_calendar(2026, 6)
    rsv_cells = [
        c
        for week in data.weeks
        for c in week
        if c.in_month and c.duty_class == "rsv"
    ]
    assert len(rsv_cells) == 8     # FISHER has 8 reserve days in June
    assert all(c.assignment_id == "1021" for c in rsv_cells)
    assert all(c.pch == Decimal("3.82") for c in rsv_cells)


def test_calendar_legend_includes_observed_classes():
    data = load_calendar(2026, 6)
    labels = {entry.label for entry in data.legend}
    # June for FISHER has FLT, RSV, OFF (no training/PTO etc.)
    assert "FLT" in labels
    assert "RSV" in labels


def test_calendar_footer_matches_engine_monthly_pch():
    data = load_calendar(2026, 6)
    assert data.monthly_pch == Decimal("65.78")
    assert data.line_value == Decimal("65.78")
    assert data.delta_vs_mpg == Decimal("0.78")


def test_calendar_padding_cells_marked_out_of_month():
    data = load_calendar(2026, 5)
    # May 1 2026 is a Friday → week 1 starts Mon Apr 27, four padding days
    first_week = data.weeks[0]
    out_count = sum(1 for c in first_week if not c.in_month)
    assert out_count == 4


# ── Flag flow: reassigned + callout via synthetic Month ────────────────


def _pilot() -> PilotProfile:
    return PilotProfile(
        pilot_id="DFI",
        name="Dennis FISHER",
        position=Position.FO,
        hourly_rate=Decimal("124.59"),
    )


def test_calendar_surfaces_reassigned_flag_via_pipeline_cache(monkeypatch):
    """If the pipeline result carries a Trip with versions (a reassignment),
    the calendar cell for that date shows is_reassigned=True. We poke the
    cached pipeline to inject a synthetic reassignment without changing
    on-disk data."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)

    # Inject a version on FISHER's June 12 "768" trip.
    new_trips: list[Trip] = []
    for trip in real.updated_month.trips:
        if trip.trip_id == "768" and date(2026, 6, 12) in trip.dates:
            new_trips.append(
                Trip(
                    **{
                        **{f.name: getattr(trip, f.name) for f in trip.__dataclass_fields__.values()},
                        "versions": (
                            AssignmentVersion(seq=1, pch_value=Decimal("5.00"), label="test"),
                        ),
                    }
                )
            )
        else:
            new_trips.append(trip)
    poked = Month(
        pilot=real.updated_month.pilot,
        year=real.updated_month.year,
        month=real.updated_month.month,
        line_value=real.updated_month.line_value,
        trips=tuple(new_trips),
        days=real.updated_month.days,
    )
    poked_result = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=poked,
        engine_result=compute_pay(lower_month(poked)),
        applied_events=real.applied_events,
        validation_discrepancies=real.validation_discrepancies,
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked_result):
        data = load_calendar(2026, 6)

    cell_june_12 = next(
        c
        for week in data.weeks
        for c in week
        if c.date == date(2026, 6, 12)
    )
    assert cell_june_12.is_reassigned is True


def test_calendar_surfaces_callout_flag(monkeypatch):
    """A Day with callout_trip_pch set should render as a 'CALLOUT' cell
    visually styled FLT, with has_callout=True. We poke the cached pipeline."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)

    # Pick FISHER's June 16 reserve day and set callout_trip_pch.
    new_days: list[Day] = []
    for day in real.updated_month.days:
        if day.date == date(2026, 6, 16) and day.duty_type is DutyType.RSV:
            new_days.append(
                Day(
                    date=day.date,
                    duty_type=day.duty_type,
                    pch_value=day.pch_value,
                    reason_code=day.reason_code,
                    premium_category=day.premium_category,
                    workdays=day.workdays,
                    callout_trip_pch=Decimal("4.50"),
                    callout_trip_id="720/1780",
                    label=day.label,
                )
            )
        else:
            new_days.append(day)
    poked = Month(
        pilot=real.updated_month.pilot,
        year=real.updated_month.year,
        month=real.updated_month.month,
        line_value=real.updated_month.line_value,
        trips=real.updated_month.trips,
        days=tuple(new_days),
    )
    poked_result = type(real)(
        pilot=real.pilot,
        year=real.year,
        month=real.month,
        updated_month=poked,
        engine_result=compute_pay(lower_month(poked)),
        applied_events=real.applied_events,
        validation_discrepancies=real.validation_discrepancies,
        feed=real.feed,
        reconciliation=real.reconciliation,
        packet=real.packet,
        packet_trip_count=real.packet_trip_count,
        fa_loaded=True,
        packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked_result):
        data = load_calendar(2026, 6)

    cell_june_16 = next(
        c
        for week in data.weeks
        for c in week
        if c.date == date(2026, 6, 16)
    )
    assert cell_june_16.has_callout is True
    assert cell_june_16.duty_label == "CALLOUT"
    assert cell_june_16.duty_class == "flt"
    # The flown trip is the bold "new" assignment; the reserve line stays as
    # the (subtle) original — a distinct, non-empty designator.
    assert cell_june_16.new_assignment_id == "720/1780"
    assert cell_june_16.assignment_id
    assert cell_june_16.assignment_id != "720/1780"
    assert cell_june_16.pch == Decimal("4.50")  # max(DPG 3.82, 4.50)
