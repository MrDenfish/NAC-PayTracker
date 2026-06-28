"""Day detail view tests."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import load_day


client = TestClient(app)


# ── Route happy paths ──────────────────────────────────────────────────


def test_day_route_renders_flt_day():
    r = client.get("/day/2026-06-12")
    assert r.status_code == 200
    assert "Friday, June 12, 2026" in r.text
    assert "Dennis FISHER" in r.text
    # Trip aid is "768" with packet cross-reference
    assert "768" in r.text
    assert "768/768/769" in r.text
    # Effective PCH
    assert "4.17" in r.text


def test_day_route_renders_reserve_day():
    r = client.get("/day/2026-06-16")
    assert r.status_code == 200
    assert "Tuesday, June 16, 2026" in r.text
    assert "1021" in r.text
    assert "RSV" in r.text
    assert "3.82" in r.text


def test_day_route_renders_off_day():
    r = client.get("/day/2026-06-07")
    assert r.status_code == 200
    assert "Sunday, June 7, 2026" in r.text
    assert "No scheduled activity" in r.text


def test_day_route_invalid_iso_returns_400():
    r = client.get("/day/not-a-date")
    assert r.status_code == 400


def test_day_route_unknown_month_returns_404():
    """Date that's valid ISO but month isn't in _DOC_INDEX."""
    r = client.get("/day/2030-01-15")
    assert r.status_code == 404


def test_day_route_active_nav_is_calendar():
    """Day detail is reached from the calendar — keep Calendar highlighted,
    and the Calendar tab must carry the viewed month so it doesn't snap to
    the newest available month on click."""
    r = client.get("/day/2026-06-12")
    assert 'href="/calendar?ym=2026-6" class="nav-link nav-link--active"' in r.text


def test_day_route_nav_links_preserve_month():
    """All month-scoped nav tabs carry the viewed month (?ym=) so switching
    tabs from a June day stays on June."""
    r = client.get("/day/2026-06-12")
    for path in ("/?ym=2026-6", "/calendar?ym=2026-6", "/pay?ym=2026-6",
                 "/compare?ym=2026-6", "/discrepancies?ym=2026-6"):
        assert f'href="{path}"' in r.text
    # Non-month-scoped tabs stay bare.
    assert 'href="/settings"' in r.text
    assert 'href="/documents"' in r.text


# ── Loader content ─────────────────────────────────────────────────────


def test_load_day_flt_pulls_packet_components():
    """June 12 = FLT 768, packet trip 768/768/769 with the four printed
    components. Flight Op should win the max."""
    d = load_day(2026, 6, 12)
    assert d.kind == "trip"
    assert d.assignment_id == "768"
    assert d.packet_trip_id == "768/768/769"
    assert d.in_packet is True
    assert d.effective_pch == Decimal("4.17")
    assert d.published_pch == Decimal("4.17")
    assert d.pch_uplift == Decimal("0")

    labels = {c.label for c in d.packet_components}
    assert {"Flight Operation", "Duty Rig", "Trip Rig", "Cumulative DPG", "Deadhead"} <= labels
    winning = [c for c in d.packet_components if c.is_winning]
    assert len(winning) == 1
    assert winning[0].label == "Flight Operation"
    assert winning[0].pch == Decimal("4.17")


def test_load_day_flt_includes_ical_legs():
    """June 12 has 3 legs in the iCal sample (768 ANC-BRW, 768 BRW-SCC,
    769 SCC-ANC) — the loader should expose them as DayLegs in order."""
    d = load_day(2026, 6, 12)
    assert len(d.legs) == 3
    assert [(leg.flight_no, leg.origin, leg.destination) for leg in d.legs] == [
        ("768", "ANC", "BRW"),
        ("768", "BRW", "SCC"),
        ("769", "SCC", "ANC"),
    ]
    # Total actual block matches sch_block exactly when nothing extended.
    assert d.actual_block_hours == d.sch_block_hours
    assert d.block_delta == Decimal("0")


def test_load_day_rsv_has_no_packet_or_legs():
    d = load_day(2026, 6, 16)
    assert d.kind == "reserve"
    assert d.duty_label == "RSV"
    assert d.duty_class == "rsv"
    assert d.assignment_id == "1021"
    assert d.effective_pch == Decimal("3.82")
    assert d.published_pch == Decimal("3.82")
    assert d.packet_components == ()
    assert d.legs == ()
    assert d.callout_trip_pch is None


def test_load_day_off_returns_kind_off_with_empty_fields():
    d = load_day(2026, 6, 7)
    assert d.kind == "off"
    assert d.duty_label == "OFF"
    assert d.assignment_id is None
    assert d.effective_pch is None
    assert d.published_pch is None
    assert d.packet_components == ()
    assert d.legs == ()


def test_load_day_navigation_has_prev_and_next():
    d = load_day(2026, 6, 12)
    assert d.prev_date_iso == "2026-06-11"
    assert d.next_date_iso == "2026-06-13"
    assert d.back_to_calendar_url == "/calendar?ym=2026-6"


def test_load_day_invalid_date_raises():
    import pytest
    with pytest.raises(ValueError):
        load_day(2026, 2, 31)


def test_calendar_cells_link_to_day_route():
    """The calendar grid wraps each in-month cell in <a href="/day/{date}">.
    Verify links are present for known FLT dates."""
    r = client.get("/calendar?ym=2026-6")
    assert r.status_code == 200
    assert 'href="/day/2026-06-12"' in r.text
    assert 'href="/day/2026-06-17"' in r.text
    # OFF cells too (e.g. June 7)
    assert 'href="/day/2026-06-07"' in r.text


def test_day_detail_callout_header_shows_flown_trip():
    """Regression (June 27 bug): the day-detail Assignment header must surface
    the flown callout trip (callout_trip_id) — like the calendar — not the bare
    reserve line. Previously _build_day_detail only fell back to day.label, so
    the day page showed "1021" while the calendar showed the flown trip."""
    from datetime import date
    from unittest.mock import patch

    from nac_pay.app.services import _pipeline
    from nac_pay.engine import compute_pay
    from nac_pay.schedule import Day, DutyType, Month, lower_month

    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    new_days = []
    for day in real.updated_month.days:
        if day.date == date(2026, 6, 16) and day.duty_type is DutyType.RSV:
            new_days.append(
                Day(
                    date=day.date, duty_type=day.duty_type, pch_value=day.pch_value,
                    reason_code=day.reason_code, premium_category=day.premium_category,
                    workdays=day.workdays, callout_trip_pch=Decimal("6.08"),
                    callout_trip_id="720/723/1780/1781", label="1021",
                )
            )
        else:
            new_days.append(day)
    poked = Month(
        pilot=real.updated_month.pilot, year=real.updated_month.year,
        month=real.updated_month.month, line_value=real.updated_month.line_value,
        trips=real.updated_month.trips, days=tuple(new_days),
    )
    poked_result = type(real)(
        pilot=real.pilot, year=real.year, month=real.month, updated_month=poked,
        engine_result=compute_pay(lower_month(poked)),
        applied_events=real.applied_events,
        validation_discrepancies=real.validation_discrepancies, feed=real.feed,
        reconciliation=real.reconciliation, packet=real.packet,
        packet_trip_count=real.packet_trip_count, fa_loaded=True, packet_loaded=True,
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked_result):
        d = load_day(2026, 6, 16)

    # Header surfaces the flown trip; the reserve line remains the history
    # "Original" baseline (a distinct, non-empty designator).
    assert d.assignment_id == "720/723/1780/1781"
    assert d.duty_label == "CALLOUT"
