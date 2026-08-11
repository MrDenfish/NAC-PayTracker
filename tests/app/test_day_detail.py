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
