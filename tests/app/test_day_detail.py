"""Day detail view tests."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import _pipeline, load_day
from nac_pay.engine import compute_pay
from nac_pay.schedule import AssignmentVersion, lower_month
from nac_pay.schedule.apply_actuals import (
    REASSIGN_CONFIRMED,
    REASSIGN_PROPOSED,
    REASSIGN_REJECTED,
    FeedReassignment,
)


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

    # The card footer is the PACKET's own §3.E trip PCH (max component + DH),
    # not the credited effective — so it always reflects the rows above it.
    # Flown-as-scheduled here, so they coincide (no divergence note).
    non_dh = [c.pch for c in d.packet_components if c.label != "Deadhead"]
    dh = next(c.pch for c in d.packet_components if c.label == "Deadhead")
    assert d.packet_trip_pch == max(non_dh) + dh == Decimal("4.17")
    assert d.packet_trip_pch == d.effective_pch   # no actual/reassignment uplift


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


def test_day_duty_window_anchors_to_the_scheduled_show_time():
    """A delayed push must not shorten the duty day — the pilot reported at
    the scheduled show time regardless. Aug 8 2026: show 04:41, flight
    pushed to 06:00 local; duty runs 04:41 → 18:15, not 05:00 → 18:15."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)   # 06:00 AKDT
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)      # 18:00 AKDT

    w = _day_duty_window(first_out, last_in, "04:41")

    assert w.duty_on == "04:41"
    assert w.duty_off == "18:15"
    assert abs(w.duty_hours - Decimal("13.5667")) < Decimal("0.001")
    assert w.duty_rig_pch == w.duty_hours / Decimal("2")


def test_day_duty_window_falls_back_to_blockout_pad_without_a_show_time():
    """No matched packet trip (a reroute, an off-day pickup) leaves nothing
    to anchor to — keep the actual-out − 1:00 estimate."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(first_out, last_in, None)

    assert w.duty_on == "05:00"
    assert abs(w.duty_hours - Decimal("13.25")) < Decimal("0.001")


def test_load_day_duty_window_matches_padding():
    """Duty window = first leg out − 1:00 report, last leg in + 0:15, with
    duty rig = duty/2. Checked against the real June 12 iCal legs."""
    from datetime import date

    from nac_pay.app.services import _pipeline

    _pipeline.cache_clear()
    pr = _pipeline(2026, 6)
    legs = sorted(
        (l for l in pr.feed.flight_legs if l.dt_start_utc.date() == date(2026, 6, 12)),
        key=lambda l: l.dt_start_utc,
    )
    span_h = (legs[-1].dt_end_utc - legs[0].dt_start_utc).total_seconds() / 3600
    expected_duty = Decimal(str(span_h)) + Decimal("1.25")  # 1:00 + 0:15 pad

    d = load_day(2026, 6, 12)
    assert d.duty_on and d.duty_off          # non-empty local "HH:MM"
    assert abs(d.duty_hours - expected_duty) < Decimal("0.001")
    assert d.duty_rig_pch == d.duty_hours / Decimal("2")
    # Duty always exceeds pure flying (ground time + padding).
    assert d.duty_hours > d.actual_block_hours
    # Legs carry an Anchorage-local out/in string.
    assert all(leg.out_local and leg.in_local for leg in d.legs)


def test_load_day_pch_candidates_hierarchy():
    """A flown day exposes its PCH candidates with exactly one marked as the
    credited (effective) value, and the footer equals effective_pch."""
    d = load_day(2026, 6, 12)
    assert d.pch_candidates
    labels = [c.label for c in d.pch_candidates]
    assert any("Flight-op" in x for x in labels)
    assert any("Duty-rig" in x for x in labels)
    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].pch == d.effective_pch


def _fr(
    date_,
    *,
    status: str = REASSIGN_CONFIRMED,
    override_pch: Decimal | None = None,
    new_pch: Decimal = Decimal("4.17"),
    signature: str = "768/769",
) -> FeedReassignment:
    """Build a ``FeedReassignment`` the way ``apply_actuals_to_month``
    would (Task 1's fold: credited = max(override, new_pch) when an
    override is present, else new_pch). ``effective_pch``/``applied``
    mirror production for every status, not just the CONFIRMED default:
    apply_actuals's REROUTE branch computes
    ``effective = max(published, credited)`` and ``applied=True``
    identically for CONFIRMED and PROPOSED — status only gates the UI
    badge, not the value. Only REJECTED is a different code path, which
    pins the day back to the published original and never applies."""
    original_pch = Decimal("4.17")
    credited = new_pch if override_pch is None else max(override_pch, new_pch)
    effective_pch = (
        original_pch if status == REASSIGN_REJECTED
        else max(original_pch, credited)
    )
    return FeedReassignment(
        date=date_,
        signature=signature,
        original_aid="768",
        original_pch=original_pch,
        new_pch=new_pch,
        effective_pch=effective_pch,
        status=status,
        applied=(status != REASSIGN_REJECTED),
        override_pch=override_pch,
    )


def _poked_pipeline(feed_reassignments: tuple, credited_pch: Decimal):
    """June 12 / trip 768, folded with ``credited_pch`` as its winning
    ``AssignmentVersion`` (mirroring ``apply_actuals_to_month``'s real
    max() fold from Task 1), plus the given ``feed_reassignments`` tuple —
    poked onto the real June pipeline result because no unmatched feed
    trip exists in the bundled corpus to trigger a real CONFIRMED reroute
    (same "poke the cached pipeline" precedent as
    ``test_calendar.py::test_calendar_surfaces_reassigned_flag_via_pipeline_cache``)."""
    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    new_trips = []
    for trip in real.updated_month.trips:
        if trip.trip_id == "768" and date(2026, 6, 12) in trip.dates:
            new_trips.append(replace(
                trip,
                versions=trip.versions + (
                    AssignmentVersion(
                        seq=len(trip.versions) + 1,
                        pch_value=credited_pch,
                        label="company-assigned (test)",
                    ),
                ),
            ))
        else:
            new_trips.append(trip)
    poked_month = replace(real.updated_month, trips=tuple(new_trips))
    return replace(
        real,
        updated_month=poked_month,
        engine_result=compute_pay(lower_month(poked_month)),
        feed_reassignments=feed_reassignments,
    )


def test_candidates_card_includes_the_company_assigned_row():
    """Aug 10 class of defect: with a company PCH entered the card listed
    three candidates, marked NO winner, and asserted a fourth number that
    appeared nowhere. The company value must be a row, and exactly one
    row must be marked winning.

    Also mutation coverage for ``load_day``'s ``fr_for_day`` resolution,
    which now reads ``fr.date == target and fr.status != REASSIGN_REJECTED``
    (Finding 2, 2026-08-11: PROPOSED folds identically to CONFIRMED, so the
    gate widened from "== CONFIRMED" to "!= REJECTED"). Two distractors,
    ordered BEFORE the real one, each isolate one clause:
      - ``distractor_rejected_same_date``: correct date, REJECTED status —
        picked up first if the ``!= REASSIGN_REJECTED`` clause is deleted
        (leaving only the date clause).
      - ``distractor_confirmed_other_date``: wrong date, CONFIRMED status —
        picked up first if the ``fr.date == target`` clause is deleted
        (leaving only the status clause).
    Either mutation leaks a distractor's value onto the card instead of the
    real winner's 5.17."""
    winner_fr = _fr(date(2026, 6, 12), override_pch=Decimal("5.17"))
    distractor_rejected_same_date = _fr(
        date(2026, 6, 12), status=REASSIGN_REJECTED,
        override_pch=Decimal("9.99"), signature="768/771",
    )
    distractor_confirmed_other_date = _fr(
        date(2026, 6, 13), override_pch=Decimal("8.88"), signature="769/770",
    )
    poked = _poked_pipeline(
        (distractor_rejected_same_date, distractor_confirmed_other_date, winner_fr),
        credited_pch=Decimal("5.17"),
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_day(2026, 6, 12)
        r = client.get("/day/2026-06-12")

    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" in labels
    # This is a REROUTE fixture (fr.kind defaults to REASSIGN_KIND_REROUTE,
    # not OFF_DAY_PICKUP) — the "Published" row must stay "Published", not
    # get relabeled "Pickup (credited)" (that relabel is pickup-only).
    assert "Published" in labels
    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].pch == d.effective_pch
    assert winners[0].label == "Company-assigned (reassignment notice)"
    # Neither distractor's value leaked onto the June 12 card.
    values = {str(c.pch.quantize(Decimal("0.01"))) for c in d.pch_candidates}
    assert "9.99" not in values
    assert "8.88" not in values

    # Minor 4: assert on the rendered HTML too, not just the loader record —
    # pins the template loop (day.html's generic `for c in
    # data.pch_candidates`) as well as the loader.
    assert r.status_code == 200
    assert "Company-assigned (reassignment notice)" in r.text


def test_candidates_card_marks_recompute_winner_on_a_proposed_reassignment():
    """Finding 2 (2026-08-11): apply_actuals folds a PROPOSED reassignment
    into effective_pch exactly like a CONFIRMED one — status only gates the
    confirm/reject badge. Before the fix, ``fr_for_day`` required
    ``status == REASSIGN_CONFIRMED``, so a PROPOSED reroute whose recompute
    wins (trip-rig/DPG/deadhead-driven) marked ZERO winners on this card
    while the reassignment card on the same page marked "Recomputed ←
    credited" — the original Aug 10 defect, one status over. No company
    value is entered, so only the Recomputed row is new; it alone must be
    marked winning."""
    fr = _fr(
        date(2026, 6, 12), status=REASSIGN_PROPOSED,
        override_pch=None, new_pch=Decimal("5.05"),
    )
    assert fr.status == REASSIGN_PROPOSED and fr.applied is True   # sanity
    assert fr.effective_pch == Decimal("5.05")
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.05"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_day(2026, 6, 12)

    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" not in labels
    assert "Recomputed from actual times (max of the §3.E components)" in labels
    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].label == "Recomputed from actual times (max of the §3.E components)"
    assert winners[0].pch == d.effective_pch == Decimal("5.05")


def test_candidates_card_recompute_row_wins_when_it_beats_the_company_value():
    """I2: Task 1 made recompute-beats-company reachable — when the
    recompute wins (trip-rig/DPG/deadhead-driven, not necessarily equal to
    any other row already on the card), the card must still mark exactly
    one winner instead of the zero-winner defect one branch over from the
    company row. Company 4.80 loses to recompute 5.05."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("4.80"), new_pch=Decimal("5.05"))
    assert fr.effective_pch == Decimal("5.05")   # max(4.80, 5.05) — sanity
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.05"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_day(2026, 6, 12)

    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" in labels
    # Minor 4: Card A's recompute row carries the same "(max of the §3.E
    # components)" qualifier as Card B's, so the two cards describe the
    # row identically.
    assert "Recomputed from actual times (max of the §3.E components)" in labels
    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].label == "Recomputed from actual times (max of the §3.E components)"
    assert winners[0].pch == d.effective_pch == Decimal("5.05")


def test_candidates_card_recompute_wins_a_tie_with_flight_op_confirmed_no_override():
    """Minor 5: a CONFIRMED FR with ``override_pch=None`` — no company
    value entered — is the one display change that fires on REAL prod
    days today (no pilot has to type anything for this row to appear).
    The recompute row must appear (no Company-assigned row), and when it
    ties the actual-block ("Flight-op") candidate, the recompute row —
    listed first in ``raw`` — takes the mark, not Flight-op. Published is
    poked away from the tie value (3.00) so it can't also claim it; this
    isolates the Recomputed-vs-Flight-op ordering the fix actually
    changed, independent of the pre-existing Published/Flight-op tie
    (both 4.17) that ``_poked_pipeline``'s unmodified fixture carries."""
    from dataclasses import replace as _replace

    _pipeline.cache_clear()
    real = _pipeline(2026, 6)
    tie_value = Decimal("4.17")   # quantizes-ties the real Flight-op 4.1666...
    fr = _fr(
        date(2026, 6, 12), status=REASSIGN_CONFIRMED,
        override_pch=None, new_pch=tie_value,
    )
    new_trips = []
    for trip in real.updated_month.trips:
        if trip.trip_id == "768" and date(2026, 6, 12) in trip.dates:
            new_trips.append(_replace(
                trip,
                published_pch=Decimal("3.00"),
                versions=trip.versions + (
                    AssignmentVersion(
                        seq=len(trip.versions) + 1,
                        pch_value=tie_value,
                        label="company-assigned (test)",
                    ),
                ),
            ))
        else:
            new_trips.append(trip)
    poked_month = _replace(real.updated_month, trips=tuple(new_trips))
    poked = _replace(
        real,
        updated_month=poked_month,
        engine_result=compute_pay(lower_month(poked_month)),
        feed_reassignments=(fr,),
    )

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        d = load_day(2026, 6, 12)

    assert d.effective_pch == Decimal("4.17")
    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" not in labels
    assert "Recomputed from actual times (max of the §3.E components)" in labels
    assert any(l.startswith("Flight-op") for l in labels)   # sanity: it's on the card

    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert winners[0].label == "Recomputed from actual times (max of the §3.E components)"
    assert winners[0].pch == d.effective_pch


def test_candidates_card_without_a_company_value_is_unchanged():
    """No-override safety: with no CONFIRMED feed reassignment for the
    viewed date, ``fr_for_day`` resolves to ``None`` and both new
    ``raw.append`` calls are fully guarded by ``fr_for_day is not None`` —
    structurally, nothing about the card's construction changes. Neither
    new row is present."""
    _pipeline.cache_clear()
    d = load_day(2026, 6, 12)
    labels = [c.label for c in d.pch_candidates]
    assert "Company-assigned (reassignment notice)" not in labels
    assert not any(l.startswith("Recomputed from actual times") for l in labels)


# ── Reassignment card: comparison table + amend-form links (Task 3) ────


def _reassignment_card_html(html: str) -> str:
    """Slice out just the feed_reassignment card's rendered HTML. The Times
    and Legs cards further down the same page already contain their own
    "?duty=1#reassign-form" / "?amend=1#reassign-form" links for any flown,
    editable day — without scoping to this card, link assertions could pass
    against those unrelated cards even if the reassignment card itself never
    grew the new links."""
    start = html.index("Company reassignment detected")
    end = html.index('<h2 class="card-title">', start + 1)
    return html[start:end]


def _comparison_table_html(card: str) -> str:
    """Slice out just the option-table itself. Scoping to the table (not
    just the card) matters because the CONFIRMED banner and the pch input's
    ``value=`` attribute elsewhere in the same card also echo the
    company-assigned figure — a mutation that deletes the table's company
    row wouldn't otherwise be caught, since "5.17" would still be found in
    the card from those other spots."""
    start = card.index('<table class="option-table">')
    end = card.index("</table>", start) + len("</table>")
    return card[start:end]


def test_reassignment_card_shows_the_comparison_rows():
    """The comparison table lists all three §3.E.1.b candidates with their
    values, replacing the old prose paragraph."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("5.17"), new_pch=Decimal("5.05"))
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.17"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        r = client.get("/day/2026-06-12")

    table = _comparison_table_html(_reassignment_card_html(r.text))
    assert "Original" in table and "4.17" in table
    assert "Company-assigned" in table and "5.17" in table
    assert "Recomputed from actual times" in table and "5.05" in table


_ROW_RE = re.compile(r'<tr class="([^"]*)">\s*<td>([^<]*)</td>')


def _table_rows(table_html: str) -> list[tuple[str, bool]]:
    """Parse each ``<tr class="...">`` row's label and whether it actually
    carries the ``winning`` CSS class — a substring check on the label text
    alone can't tell "marked winning" from "just present", which is exactly
    the code path (the ``namespace(marked=...)`` first-match logic) this
    task's winner-marking rule lives in."""
    return [
        (label.strip(), "winning" in cls.split())
        for cls, label in _ROW_RE.findall(table_html)
    ]


def test_reassignment_card_winner_marking_company_wins():
    """Company (5.17) beats recompute (5.05): the Company row alone is
    marked winning."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("5.17"), new_pch=Decimal("5.05"))
    assert fr.effective_pch == Decimal("5.17")
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.17"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        r = client.get("/day/2026-06-12")

    rows = _table_rows(_comparison_table_html(_reassignment_card_html(r.text)))
    winners = [label for label, win in rows if win]
    assert len(winners) == 1
    assert winners[0].startswith("Company-assigned")


def test_reassignment_card_winner_marking_recompute_wins():
    """Recompute (5.05) beats company (4.80): the Recomputed row alone is
    marked winning."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("4.80"), new_pch=Decimal("5.05"))
    assert fr.effective_pch == Decimal("5.05")
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.05"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        r = client.get("/day/2026-06-12")

    rows = _table_rows(_comparison_table_html(_reassignment_card_html(r.text)))
    winners = [label for label, win in rows if win]
    assert len(winners) == 1
    assert winners[0].startswith("Recomputed from actual times")


def test_reassignment_card_winner_marking_tie_prefers_company():
    """Company == recompute == effective (5.05 all three): Task 2's tie
    precedence marks Company, and exactly one row is marked — not both."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("5.05"), new_pch=Decimal("5.05"))
    assert fr.effective_pch == Decimal("5.05")
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.05"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        r = client.get("/day/2026-06-12")

    rows = _table_rows(_comparison_table_html(_reassignment_card_html(r.text)))
    winners = [label for label, win in rows if win]
    assert len(winners) == 1
    assert winners[0].startswith("Company-assigned")


def test_reassignment_card_links_into_the_amend_form():
    """Two navigation links point into the one existing amend form — no new
    input/write path on this card."""
    fr = _fr(date(2026, 6, 12), override_pch=Decimal("5.17"), new_pch=Decimal("5.05"))
    poked = _poked_pipeline((fr,), credited_pch=Decimal("5.17"))

    with patch("nac_pay.app.services._pipeline", return_value=poked):
        r = client.get("/day/2026-06-12")

    card = _reassignment_card_html(r.text)
    assert "?duty=1#reassign-form" in card
    assert "?amend=1#reassign-form" in card


def test_load_day_exposes_scheduled_duty_window_from_packet():
    """Reconstruct-from-packet: a day with a matched packet trip carries the
    scheduled duty window (local HH:MM) + scheduled duty rig, independent of
    iCal legs — the reliable fallback when feed legs have aged out."""
    d = load_day(2026, 6, 12)
    assert d.sched_duty_on and d.sched_duty_off
    assert len(d.sched_duty_on) == 5 and d.sched_duty_on[2] == ":"
    assert d.sched_duty_rig_pch is not None and d.sched_duty_rig_pch > 0


# ── Reason tag + premium/absence color on the Assignment card ───────────


def _post_override(date_iso: str, reason: str, premium: str, custom: str = "") -> None:
    client.post(
        f"/day/{date_iso}",
        data={"reason_code": reason, "premium_category": premium,
              "entry_mode": "SIMPLE", "custom_multiplier": custom},
        follow_redirects=False,
    )


def test_day_assignment_card_shows_reason_tag_and_color():
    _post_override("2026-06-02", "SICK", "NONE")
    body = client.get("/day/2026-06-02").text
    assert ">SICK<" in body
    assert "duty-bg--absence" in body


def test_day_assignment_card_premium_green_keeps_flt_tag():
    _post_override("2026-06-02", "FLOWN", "OVERTIME")
    body = client.get("/day/2026-06-02").text
    assert "duty-bg--premium" in body
    assert ">FLT<" in body


# ── Duty-time override wiring (Task 6) ───────────────────────────────────
#
# The day page must show the SAME duty window the pilot was actually paid
# on. Tasks 4/5 already made a stored DUTY_CORRECTION drive the CREDITED
# §3.E recompute (Trip.effective_pch); these tests pin the display side —
# _day_duty_window's own tier-1 precedence, and the two real bugs a
# reviewer found on the live code path: (1) the duty card kept sourcing
# "Duty-rig (actual)" from raw feed legs even with a correction saved, and
# (2) because of that mismatch, no PCH candidate ever quantized equal to
# the corrected effective_pch, so the components card marked NO winner.


def test_day_duty_window_prefers_the_pilot_override():
    """Tier 1 beats the packet show time (tier 2) and the actual-out
    fallback (tier 3)."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(first_out, last_in, "04:41", ("05:15", "17:45", None))

    assert w.duty_on == "05:15"
    assert w.duty_off == "17:45"
    assert abs(w.duty_hours - Decimal("12.50")) < Decimal("0.001")
    assert w.duty_rig_pch == w.duty_hours / Decimal("2")
    assert w.is_override is True


def test_day_duty_window_ignores_a_half_filled_override():
    """One clock without the other (and no stored duty_hours fallback) is
    not a window — fall back to tier 2."""
    from datetime import datetime, timezone

    from nac_pay.app.services import _day_duty_window

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(first_out, last_in, "04:41", ("05:15", "", None))

    assert w.duty_on == "04:41"
    assert w.is_override is False


def test_day_duty_window_falls_back_to_stored_hours_when_the_override_has_no_clocks():
    """Fix-round-1 IMPORTANT 1, tier 1b: a stored DUTY_CORRECTION can carry
    duty_hours with NO clocks — UserAssignmentVersionStore.save accepts
    duty_on_local=None, and the engine's _actual_duty_hours already credits
    such a correction outright (it only needs duty_hours). The display must
    show the SAME duty_hours the pilot is credited on rather than silently
    falling through to the packet show time and disagreeing with the pay.
    Back-anchors to the actual last block-in + TRIP_END_PAD (mirroring
    tiers 2/3's own back anchor, and the manual-legs branch's duty_off −
    duty_hours reconstruction) since there's no clock pair to render
    exactly.

    Mutation-verified: deleting the ``if ov_hours is not None:`` branch in
    ``_day_duty_window`` makes this FAIL (falls through to tier 2:
    duty_hours becomes ~12h from the packet show time instead of 20.00)."""
    from datetime import datetime, timedelta, timezone

    from nac_pay.app.services import _day_duty_window
    from nac_pay.timeutil import DOMICILE_TZ

    first_out = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    last_in = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)

    w = _day_duty_window(
        first_out, last_in, "04:41", (None, None, Decimal("20.00")),
    )

    assert w.duty_hours == Decimal("20.00")
    assert w.duty_rig_pch == Decimal("10.00")
    assert w.is_override is True
    expected_off = (last_in + timedelta(minutes=15)).astimezone(DOMICILE_TZ)
    expected_on = expected_off - timedelta(hours=20)
    assert w.duty_off == expected_off.strftime("%H:%M")
    assert w.duty_on == expected_on.strftime("%H:%M")


def _save_duty_correction(
    date_iso: str,
    *,
    duty_on_local: str,
    duty_off_local: str,
    duty_hours: Decimal,
    pch_value: Decimal,
    assignment_id: str = "768",
) -> None:
    from nac_pay.storage import (
        DEFAULT_USER_ID,
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    UserAssignmentVersionStore(user_id=DEFAULT_USER_ID).save(
        date_iso=date_iso,
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id=assignment_id,
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=pch_value,
        duty_hours=duty_hours,
        duty_on_local=duty_on_local,
        duty_off_local=duty_off_local,
    )


def test_load_day_shows_the_corrected_duty_window_not_the_feed_derived_one():
    """End-to-end through load_day: a saved DUTY_CORRECTION on 2026-06-12
    (the same fixture trip 768 used by test_duty_correction_flows_into_the_
    pipeline_recompute in test_day_edit.py, where duty 20.00h → duty-rig
    10.00 beats the published 4.17) must make the DAY PAGE's duty on/off/
    hours/rig match what the pilot was actually paid on — not the raw feed
    window the card showed before this task (05:30 report, ~3h duty per
    this fixture's actual iCal legs — see the pre-fix failure output in
    task-6-report.md)."""
    from nac_pay.app.services import _pipeline

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)

    assert d.duty_on == "03:00"
    assert d.duty_off == "23:00"
    assert d.duty_hours == Decimal("20.00")
    assert d.duty_rig_pch == Decimal("10.00")
    # Sanity: this is really the corrected/credited value, not a coincidence.
    assert d.effective_pch == Decimal("10.00")


def test_load_day_pch_candidates_mark_the_corrected_duty_rig_as_winner():
    """Components-card regression: before this task, no candidate ever
    quantized equal to the corrected effective_pch (the card kept showing
    the stale feed-derived duty rig), so the winner-marking loop found
    NOTHING to mark. With the wiring fixed, exactly one candidate wins and
    it is the (corrected) Duty-rig (actual) candidate."""
    from nac_pay.app.services import _pipeline

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)

    winners = [c for c in d.pch_candidates if c.is_winning]
    assert len(winners) == 1
    assert "Duty-rig" in winners[0].label
    assert winners[0].pch == Decimal("10.00")
    assert winners[0].pch == d.effective_pch


def test_load_day_flags_duty_window_provenance_when_corrected():
    """Task 7 UI note: the day-detail data must say WHOSE numbers are on
    the duty card. A plain flown day (no correction) is feed-derived;
    once a DUTY_CORRECTION is active, the SAME fields are the pilot's own
    entry — the page must not present one as the other."""
    from nac_pay.app.services import _pipeline

    plain = load_day(2026, 6, 12)
    assert plain.duty_window_is_correction is False

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()

    corrected = load_day(2026, 6, 12)
    assert corrected.duty_window_is_correction is True


def test_day_route_labels_the_corrected_duty_window_as_pilot_entered():
    """End-to-end through the template: the duty card must say
    "pilot-corrected" rather than silently reusing the "(actual)" label
    that implies a feed observation."""
    from nac_pay.app.services import _pipeline

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()

    r = client.get("/day/2026-06-12")
    assert r.status_code == 200
    assert "pilot-corrected" in r.text
    assert "Duty-rig (pilot-corrected)" in r.text


def test_load_day_duty_override_tiebreak_uses_created_at_then_seq():
    """Two active DUTY_CORRECTION rows CAN coexist on one date — re-editing
    appends a fresh row rather than superseding the old one via
    correction_of (only VersionType.CORRECTION does that); see
    _build_duty_overrides' docstring for the same "latest active wins" rule
    applied to the engine recompute. On a same-second created_at TIE, seq
    is the only remaining signal and must resolve to the LATER save (the
    higher seq) — the compound ``(created_at, seq)`` key load_day uses,
    matching the corrections note that created_at-alone leaves this tie
    unresolved.

    Fix-round-1 IMPORTANT 2: before the fix, ``_build_duty_overrides``
    (the ENGINE's own resolver) broke this exact tie with ``created_at``
    alone. Since ``list_for_month``/``list_for_date`` both ``ORDER BY
    seq`` ascending, Python's ``max()`` on a tie keeps the FIRST maximal
    element it sees — the LOWER seq (seq=1, 8.00h) — while the display
    (already keyed on ``(created_at, seq)``) picked seq=2 (20.00h). So the
    page showed 20.00h/10.00 duty-rig while the pay was actually only
    4.00 (which doesn't even beat published 4.17) — asserting on
    ``effective_pch`` here as well as the clocks pins BOTH sides together.

    Rows are inserted directly via the ORM (bypassing
    UserAssignmentVersionStore.save, which stamps created_at itself and
    can't be made to produce a real tie) so both rows carry the exact same
    created_at string."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import DEFAULT_USER_ID
    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserAssignmentVersionRow, UserRow

    tie = "2026-06-10T12:00:00"
    with session_scope() as sess:
        if sess.get(UserRow, DEFAULT_USER_ID) is None:
            sess.add(UserRow(user_id=DEFAULT_USER_ID))
            sess.flush()
        sess.add(UserAssignmentVersionRow(
            user_id=DEFAULT_USER_ID, date_iso="2026-06-12", seq=1,
            version_type="DUTY_CORRECTION", correction_of=None,
            assignment_id="768", entry_mode="DETAILED",
            pch_value=Decimal("4.17"),
            duty_hours=Decimal("8.00"),
            duty_on_local="01:00", duty_off_local="09:00",
            reason_code="FLOWN", premium_category="NONE", notes="",
            created_at=tie,
        ))
        sess.add(UserAssignmentVersionRow(
            user_id=DEFAULT_USER_ID, date_iso="2026-06-12", seq=2,
            version_type="DUTY_CORRECTION", correction_of=None,
            assignment_id="768", entry_mode="DETAILED",
            pch_value=Decimal("4.17"),
            duty_hours=Decimal("20.00"),
            duty_on_local="03:00", duty_off_local="23:00",
            reason_code="FLOWN", premium_category="NONE", notes="",
            created_at=tie,
        ))
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    assert d.duty_on == "03:00"
    assert d.duty_off == "23:00"
    assert d.effective_pch == Decimal("10.00")


def test_load_day_prefers_the_latest_correction_even_when_it_is_clockless():
    """Fix-round-1 IMPORTANT 1: the OLD resolver filtered clockless
    corrections out BEFORE taking the max, so an older correction WITH
    clocks could win the page even though the engine — which takes the
    max FIRST, then derives hours from clocks-or-stored-duty_hours — pays
    on a NEWER, clockless one. Save an older, clocked correction, then a
    newer SIMPLE-mode one that carries only duty_hours; the newer one must
    win on the page, matching what it pays.

    Mutation-verified: re-adding the ``and v.duty_on_local and
    v.duty_off_local`` filter to load_day's ``_duty_corrections`` list
    comprehension (services.py) makes this FAIL — it picks the older
    01:00-09:00/8.00h row instead."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import (
        DEFAULT_USER_ID,
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="01:00", duty_off_local="09:00",
        duty_hours=Decimal("8.00"), pch_value=Decimal("4.17"),
    )
    # Newer correction, clockless — only stored duty_hours (a SIMPLE-mode
    # entry, or any row UserAssignmentVersionStore.save allows to carry
    # duty_hours with duty_on_local=None).
    UserAssignmentVersionStore(user_id=DEFAULT_USER_ID).save(
        date_iso="2026-06-12",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="768",
        entry_mode=VersionEntryMode.SIMPLE,
        pch_value=Decimal("10.00"),
        duty_hours=Decimal("20.00"),
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)

    assert d.duty_hours == Decimal("20.00")
    assert d.duty_rig_pch == Decimal("10.00")
    assert d.effective_pch == Decimal("10.00")


def test_load_day_duty_correction_wins_over_a_manual_legs_reassignment():
    """Fix-round-1 IMPORTANT 3: a day can carry BOTH a DUTY_CORRECTION
    (which already drives the credited pay) AND a separate REASSIGNMENT
    version with pilot-entered legs (DETAILED mode) that happens to have
    the higher (pch_value, seq) and so "wins" the header/legs card. Before
    the fix, _build_day_detail's manual-legs branch unconditionally
    overwrote duty_on/off/hours/rig from those legs, reverting the page to
    a DIFFERENT window than the one the DUTY_CORRECTION is actually paid
    on. Legs may still drive the BLOCK figure; the duty window must stay
    the correction's.

    Mutation-verified: removing the ``if not duty_window_locked:`` guard
    in _build_day_detail (services.py) makes this FAIL — duty_hours reverts
    to the manual-legs-derived value instead of staying at 20.00."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import (
        DEFAULT_USER_ID,
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    store = UserAssignmentVersionStore(user_id=DEFAULT_USER_ID)

    # The DUTY_CORRECTION — seq 1, lower pch_value.
    store.save(
        date_iso="2026-06-12",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="768",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("10.00"),
        duty_hours=Decimal("20.00"),
        duty_on_local="03:00",
        duty_off_local="23:00",
    )
    # A separate REASSIGNMENT with pilot-entered legs — seq 2, HIGHER
    # pch_value, so _build_day_detail's (pch_value, seq) winner picks
    # THIS version for the header/legs card.
    reassign = store.save(
        date_iso="2026-06-12",
        version_type=VersionType.REASSIGNMENT,
        assignment_id="900",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("15.00"),
        block_hours=Decimal("6.00"),
        duty_hours=Decimal("9.00"),
    )
    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserVersionLegRow
    with session_scope() as sess:
        sess.add(UserVersionLegRow(
            user_id=DEFAULT_USER_ID, date_iso="2026-06-12", seq=reassign.seq,
            idx=0, flight="900", out_local="06:00", in_local="12:00",
        ))
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)

    # The header/legs follow the higher-pch reassignment...
    assert d.assignment_id == "900"
    # ...but the duty WINDOW stays the correction's, not the legs'.
    assert d.duty_on == "03:00"
    assert d.duty_off == "23:00"
    assert d.duty_hours == Decimal("20.00")
    assert d.duty_rig_pch == Decimal("10.00")
    # D4 (fix-round-2): BLOCK still comes from the legs even though the
    # duty WINDOW doesn't — this was the un-asserted half of the original
    # I3 fix, so moving the actual_block assignment inside the
    # duty_window_locked guard would previously have passed this whole
    # file undetected.
    assert d.actual_block_hours == Decimal("6.00")


def test_load_day_duty_correction_anchors_duty_off_on_manual_legs_when_present():
    """NEW IMPORTANT (fix round 2): tier 1b (an hours-only correction — the
    common case, since no write path stores clocks yet; main.py's
    reassign/correct route saves DETAILED-mode corrections as duty_hours
    with duty_on_local/duty_off_local left None) anchors duty_off on the
    FEED's last block-in + pad, because _day_duty_window has no visibility
    into manual legs. But the real write path lets that SAME correction
    carry its own pilot-entered legs (VersionLeg rows under the same seq),
    and those are the better anchor when present. The correction still
    supplies the DURATION (duty_hours/duty_rig_pch); only the on/off clock
    split should shift to the manual-leg anchor.

    Uses trip 768 on 2026-06-12 — a date with REAL feed legs — specifically
    so the feed-anchored duty_off (whatever the actual iCal last-block-in
    happens to be) is verifiably DIFFERENT from the manual-leg-anchored one
    (14:15), proving the anchor actually moved rather than coincidentally
    matching.

    Mutation-verified: reverting the ``elif duty_window_needs_leg_anchor:``
    branch in ``_build_day_detail`` (services.py) to a no-op makes this
    FAIL — duty_off reverts to the feed-anchored value instead of 14:15."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import (
        DEFAULT_USER_ID,
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionLeg,
        VersionType,
    )

    store = UserAssignmentVersionStore(user_id=DEFAULT_USER_ID)
    saved = store.save(
        date_iso="2026-06-12",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="768",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("10.00"),
        duty_hours=Decimal("20.00"),
        # No duty_on_local / duty_off_local — hours only (tier 1b).
    )
    store.save_legs(
        "2026-06-12", saved.seq,
        [VersionLeg(flight="768", out_local="03:00", in_local="14:00")],
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)

    # Sanity: this date really does carry real feed legs, so the
    # feed-anchored guess tier 1b would otherwise have used is a live
    # alternative, not a hypothetical.
    assert d.legs

    # Duration still comes from the correction...
    assert d.duty_hours == Decimal("20.00")
    assert d.duty_rig_pch == Decimal("10.00")
    assert d.effective_pch == Decimal("10.00")
    # ...but the back anchor comes from the PILOT'S manual legs (last
    # in_local 14:00 + 0:15 TRIP_END_PAD = 14:15), not the feed's last
    # block-in. duty_on is back-computed: 14:15 − 20:00 wraps to the
    # previous calendar day, rendered without a day marker (existing
    # ``% 1440`` convention, per the coordinator's D6 ruling).
    assert d.duty_off == "14:15"
    assert d.duty_on == "18:15"


# ── Fix round 1 ─────────────────────────────────────────────────────────


def test_amend_form_prefills_duty_off_from_scheduled_when_no_feed_legs(monkeypatch):
    """CRITICAL 2: the report field fell back to sched_duty_on when the
    feed had no legs, but duty-off had no such fallback — so on exactly
    the day this feature exists for (feed aged out, packet trip still
    known), the untouched form posted one clock and an empty one, and the
    route's own "Enter both duty on and duty off, or neither" validation
    then rejected it. Both-or-neither must be true by construction.

    Builds the scenario via dataclasses.replace on a REAL load_day() result
    (rather than hand-constructing the large DayDetailData) — keeps every
    other field (packet match, pilot, nav, etc.) genuinely valid, only
    nulling the feed-derived duty_on/duty_off while sched_duty_on/off (from
    the packet, independent of the feed) stay populated."""
    from dataclasses import replace

    import nac_pay.app.main as main_module

    real = load_day(2026, 6, 12)
    assert real.sched_duty_on and real.sched_duty_off, (
        "fixture assumption broken: trip 768 has no packet show time"
    )
    assert real.duty_on and real.duty_off, (
        "fixture assumption broken: trip 768 has no feed legs to null out"
    )
    no_legs = replace(
        real,
        duty_on=None, duty_off=None, duty_hours=None, duty_rig_pch=None,
        legs=(), actual_block_hours=None,
    )

    monkeypatch.setattr(main_module, "load_day", lambda *a, **kw: no_legs)
    r = client.get("/day/2026-06-12")
    assert r.status_code == 200

    m = re.search(
        r'<input type="time" id="reassign-duty-off" name="duty_off_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    assert m, "duty_off_local input not found in the rendered form"
    assert m.group(1) == real.sched_duty_off, (
        f"duty-off did not fall back to the scheduled show time: "
        f"got {m.group(1)!r}, expected {real.sched_duty_off!r}"
    )

    m2 = re.search(
        r'<input type="time" id="reassign-report" name="duty_on_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    assert m2 and m2.group(1) == real.sched_duty_on, (
        "duty-on fallback regressed alongside the duty-off fix"
    )
    # Both non-empty together — the exact "both or neither" property the
    # route's own validation depends on.
    assert m.group(1) and m2.group(1)


def test_history_never_badges_a_duty_correction_as_effective():
    """IMPORTANT 2: a DUTY_CORRECTION's pch_value is audit-only (it never
    competes in Trip.effective_pch — apply_user_versions._fold_candidates).
    Reproduces the exact review scenario: an inflated audit-only pch_value
    (15.00, as if stale block/TAFB fields were left over from a previous
    edit) on a day where the TRUE credited value is 4.17 (a short,
    corrected duty that doesn't beat published). The history list must not
    badge the 15.00 row "effective" — that's a wrong number next to a
    checkmark in the one place this app calls an audit trail."""
    from nac_pay.app.services import _pipeline

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="09:00",     # 6h -> rig 3.00
        duty_hours=Decimal("6.00"), pch_value=Decimal("15.00"),
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    assert d.effective_pch == Decimal("4.17")   # published wins; sanity

    correction_row = next(
        v for v in d.versions if v.user_version_type == "DUTY_CORRECTION"
    )
    assert correction_row.source == "Duty correction"
    assert correction_row.is_effective is False

    original_row = next(v for v in d.versions if v.seq == 0)
    assert original_row.is_effective is True
    assert original_row.pch_value == Decimal("4.17")


def test_duty_correction_no_effect_surfaced_when_no_reconciled_trip():
    """Reviewer's ruling on the disclosed item-A edge case: don't re-admit
    DUTY_CORRECTION to the max() fold to compensate (unsafe — the row's
    pch_value is pilot-submitted, not packet-derived); instead surface the
    limit. 2026-06-08 is a plain OFF day for this fixture (no trip, no Day,
    no reconciled trip at all — see test_offday_pickup_renders_confirmable_
    card in test_reassign.py) — a DUTY_CORRECTION filed there has nowhere
    for duty_overrides to land."""
    from nac_pay.app.services import _pipeline
    from nac_pay.storage import (
        DEFAULT_USER_ID,
        UserAssignmentVersionStore,
        VersionEntryMode,
        VersionType,
    )

    baseline = load_day(2026, 6, 8)
    assert baseline.kind == "off"
    assert baseline.duty_correction_no_effect is False   # nothing filed yet

    UserAssignmentVersionStore(user_id=DEFAULT_USER_ID).save(
        date_iso="2026-06-08",
        version_type=VersionType.DUTY_CORRECTION,
        assignment_id="",
        entry_mode=VersionEntryMode.DETAILED,
        pch_value=Decimal("5.00"),
        duty_hours=Decimal("10.00"),
        duty_on_local="08:00",
        duty_off_local="18:00",
    )
    _pipeline.cache_clear()

    no_trip = load_day(2026, 6, 8)
    assert no_trip.duty_correction_no_effect is True

    r = client.get("/day/2026-06-08")
    assert r.status_code == 200
    assert "isn't changing what's credited" in r.text
    assert "Reassign / amend" in r.text

    # Regression guard: a correction on a date that DOES have a reconciled
    # trip (768/2026-06-12, used throughout this module) must NOT trip the
    # same note — false positives here would train pilots to ignore it.
    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()
    covered = load_day(2026, 6, 12)
    assert covered.duty_correction_no_effect is False


def test_history_badges_nobody_when_duty_correction_drives_effective_up():
    """Fix round 2, NEW-3: the residual disclosed after the I2 fix. On an
    UPWARD correction (duty 20h -> rig 10.00, beating published 4.17 — the
    same fixture as test_duty_correction_flows_into_the_pipeline_recompute
    in test_day_edit.py), the pilot is credited 10.00, but no row's OWN
    pch_value equals 10.00 (the DUTY_CORRECTION's is excluded from
    candidates; "Original published" is 4.17). Before this fix, "Original
    published" (4.17) was wrongly badged effective. After: nobody is —
    specifically NOT "Original published", since 4.17 != the true 10.00
    credited value."""
    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    from nac_pay.app.services import _pipeline
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    assert d.effective_pch == Decimal("10.00")

    assert not any(v.is_effective for v in d.versions), (
        "some row was badged effective even though none matches the "
        "true credited value"
    )
    original_row = next(v for v in d.versions if v.seq == 0)
    assert original_row.is_effective is False
    assert original_row.pch_value == Decimal("4.17")


# ── C1: history must not render a PCH figure the pilot isn't paid ──────


def test_history_renders_duty_window_not_pch_for_duty_correction_rows():
    """C1: day.html rendered "{{ pch_value }} PCH" for every history row,
    including DUTY_CORRECTION, whose pch_value is audit-only by
    construction (see the _build_history docstring). Under the card's own
    "max across non-superseded versions" heading, a 15.00-PCH-looking row
    on a day credited 4.17 is an affirmatively misleading record. The row
    must show its corrected duty window instead, and the intro sentence
    must no longer imply every listed row competes for pay."""
    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="09:00",
        duty_hours=Decimal("6.00"), pch_value=Decimal("15.00"),
    )
    from nac_pay.app.services import _pipeline
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    correction_row = next(
        v for v in d.versions if v.user_version_type == "DUTY_CORRECTION"
    )
    assert correction_row.duty_on_local == "03:00"
    assert correction_row.duty_off_local == "09:00"

    r = client.get("/day/2026-06-12")
    assert r.status_code == 200
    # The misleading figure must not render anywhere on the page — 15.00
    # PCH is a number the pilot is never actually credited.
    assert "15.00 PCH" not in r.text
    # The corrected window renders in its place.
    assert "03:00" in r.text and "09:00" in r.text
    # The intro sentence must qualify that not every listed row competes.
    assert "compete" in r.text


def test_history_marks_the_live_duty_correction_when_more_than_one_exists():
    """I5: a DUTY_CORRECTION is never superseded (the "Correct this"
    affordance renders for REASSIGNMENT only), so re-editing appends a
    SECOND active row rather than replacing the first — neither gets
    is_effective (DUTY_CORRECTION never competes), so nothing previously
    indicated which one's created_at actually drives duty_overrides. The
    later one (by the same (created_at, seq) recency rule
    _build_duty_overrides uses) must be marked; the earlier one must not."""
    _save_duty_correction(
        "2026-06-12",
        duty_on_local="01:00", duty_off_local="09:00",
        duty_hours=Decimal("8.00"), pch_value=Decimal("4.00"),
    )
    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="09:00",
        duty_hours=Decimal("6.00"), pch_value=Decimal("3.00"),
    )
    from nac_pay.app.services import _pipeline
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    corrections = sorted(
        (v for v in d.versions if v.user_version_type == "DUTY_CORRECTION"),
        key=lambda v: v.seq,
    )
    assert len(corrections) == 2
    older, newer = corrections
    assert older.is_live_duty_correction is False
    assert newer.is_live_duty_correction is True

    r = client.get("/day/2026-06-12")
    assert r.status_code == 200
    assert ">live<" in r.text


# ── I1: badge rule scoped to days with an active DUTY_CORRECTION ──────


def test_history_badges_the_winner_even_when_effective_comes_from_elsewhere():
    """I1: on a day with NO active DUTY_CORRECTION, the badge must be
    byte-identical to main — the highest non-superseded candidate is
    ALWAYS the effective row, even when `effective` itself is driven by
    something outside the candidate list entirely (the auto iCal duty
    extension, the DPG floor, a callout's max(_DPG, callout_trip_pch)).
    Widening the == effective gate to every day (the pre-fix shape)
    silently un-badges this REASSIGNMENT row on live accounts with zero
    corrections stored — the whole-branch review's I1 finding.

    Calls _build_history directly with a synthetic REASSIGNMENT whose own
    pch_value (6.08) does NOT equal `effective` (6.625, standing in for a
    DPG-floor/callout-driven credited value) — proving the badge does not
    depend on that equality when no DUTY_CORRECTION is present."""
    from nac_pay.app.services import _build_history
    from nac_pay.storage import UserAssignmentVersion, VersionEntryMode, VersionType

    uv = UserAssignmentVersion(
        user_id="u", date_iso="2026-06-12", seq=1,
        version_type=VersionType.REASSIGNMENT, correction_of=None,
        assignment_id="722/754", entry_mode=VersionEntryMode.SIMPLE,
        pch_value=Decimal("6.08"), block_hours=None, duty_hours=None,
        tafb_hours=None, deadhead_pch=None, workdays=None,
        duty_on_local=None, duty_off_local=None,
        reason_code="REASSIGNMENT", premium_category="NONE", notes="",
        created_at="2026-06-12T00:00:00",
    )
    versions = _build_history(
        published=Decimal("4.17"),
        effective=Decimal("6.625"),   # deliberately != uv.pch_value
        user_versions=[uv],
        superseded_seqs=set(),
        superseded_by_seq={},
    )
    winner = next(v for v in versions if v.seq == 1)
    assert winner.is_effective is True, (
        "the highest non-superseded candidate must be badged when there is "
        "no active DUTY_CORRECTION on the day, regardless of whether its "
        "own pch_value equals `effective` — matches main's rule"
    )


# ── I2: amend-form clock prefill — one source, never mixed ─────────────


def test_amend_form_prefers_the_scheduled_pair_over_a_reconstructed_window(monkeypatch):
    """I2, NO-CORRECTION case: day.html:864 mixed sources — duty_on fell
    back to sched_duty_on but duty_off did NOT fall back to sched_duty_off
    (pre-C2-fix shape), and even after that fix both fields independently
    preferred the computed/actual window over the packet's scheduled pair.
    On a day whose window was reconstructed by the manual-legs branch, a
    re-amend then prefilled that reconstructed clock instead of the packet
    show, changing the JS `front` anchor and drifting duty_hours on a row
    type that DOES compete in max().

    Scoped to the case with NO active DUTY_CORRECTION, where the packet
    scheduled pair (tier 2) is still the right prefill — see
    test_amend_form_shows_the_pilot_own_correction_clocks below for the
    correction case (tier 1), which now outranks this one (NEW-1).

    The two prefills must come from ONE source: prefer the packet
    scheduled pair when BOTH halves exist, else the computed pair, else
    blank. Builds the scenario via dataclasses.replace on a REAL
    load_day() result — sched_duty_on/off stay at the packet's real show
    time (05:30/12:35) while duty_on/off are forced to a clearly
    DIFFERENT reconstructed-looking pair (18:15/14:15, mirroring the
    manual-legs-anchor shape) so the two sources are unambiguously
    distinguishable in the assertion."""
    from dataclasses import replace

    real = load_day(2026, 6, 12)
    assert real.sched_duty_on == "05:30" and real.sched_duty_off == "12:35", (
        "fixture assumption broken: trip 768's packet show time changed"
    )
    assert real.correction_duty_on is None and real.correction_duty_off is None, (
        "fixture assumption broken: this scenario must have NO active "
        "DUTY_CORRECTION so tier 2 (not tier 1) is under test"
    )
    reconstructed = replace(real, duty_on="18:15", duty_off="14:15")

    import nac_pay.app.main as main_module
    monkeypatch.setattr(main_module, "load_day", lambda *a, **kw: reconstructed)

    r = client.get("/day/2026-06-12")
    assert r.status_code == 200

    m_on = re.search(
        r'<input type="time" id="reassign-report" name="duty_on_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    m_off = re.search(
        r'<input type="time" id="reassign-duty-off" name="duty_off_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    assert m_on and m_off
    assert m_on.group(1) == "05:30", (
        "report prefill picked the reconstructed window instead of the "
        "packet's scheduled show time"
    )
    assert m_off.group(1) == "12:35", (
        "duty-off prefill picked the reconstructed window instead of the "
        "packet's scheduled show time"
    )


def test_amend_form_shows_the_pilot_own_correction_clocks():
    """NEW-1: when an active DUTY_CORRECTION exists (and the day has feed
    legs — tier 1a of _day_duty_window), the amend form must round-trip
    the PILOT'S OWN corrected clocks, not fall through to the packet's
    scheduled show time. The prior wave's I2 fix (see the test above)
    correctly stopped the form from preferring a RECONSTRUCTED window over
    the scheduled pair, but it went one step too far: it now also buries
    a genuine, live correction behind the scheduled pair, because
    data.sched_duty_on/off are checked first regardless of an active
    correction.

    Required precedence: (1) the active correction's own clocks — tier 1
    — (2) else the packet scheduled pair, (3) else the computed window,
    (4) else blank, and both halves must always come from the SAME tier.

    Failure scenario without the fix: the pilot files a correction of
    03:00-23:00, later re-opens the form with "Duty correction" selected
    to adjust ONE clock, and submits. The untouched field posts the
    PACKET's scheduled clock (05:30/12:35) instead of the pilot's own —
    because corrections are never superseded, that newest row wins
    _build_duty_overrides and the credited duty silently reverts to the
    scheduled window. Same fixture (trip 768, 2026-06-12) as
    test_load_day_shows_the_corrected_duty_window_not_the_feed_derived_one."""
    from nac_pay.app.services import _pipeline

    _save_duty_correction(
        "2026-06-12",
        duty_on_local="03:00", duty_off_local="23:00",
        duty_hours=Decimal("20.00"), pch_value=Decimal("4.17"),
    )
    _pipeline.cache_clear()

    d = load_day(2026, 6, 12)
    assert d.sched_duty_on == "05:30" and d.sched_duty_off == "12:35", (
        "fixture assumption broken: trip 768's packet show time changed"
    )

    r = client.get("/day/2026-06-12")
    assert r.status_code == 200

    m_on = re.search(
        r'<input type="time" id="reassign-report" name="duty_on_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    m_off = re.search(
        r'<input type="time" id="reassign-duty-off" name="duty_off_local"\s+'
        r'value="([^"]*)"',
        r.text,
    )
    assert m_on and m_off
    assert m_on.group(1) == "03:00", (
        "report prefill fell back to the packet's scheduled show time "
        "instead of the pilot's own live correction"
    )
    assert m_off.group(1) == "23:00", (
        "duty-off prefill fell back to the packet's scheduled show time "
        "instead of the pilot's own live correction"
    )
