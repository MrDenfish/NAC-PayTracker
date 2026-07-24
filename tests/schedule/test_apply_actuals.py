"""Tests for ``schedule.apply_actuals_to_month``.

Synthetic unit tests per event kind, plus one end-to-end integration test
against real June 2026 data (Final Award + Trip Pairing Packet + iCal feed).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.engine import compute_pay
from nac_pay.parsers import (
    FlightLegEvent,
    MatchStatus,
    ParsedFeed,
    ReconciledTrip,
    ReconciliationResult,
    TripPairing,
    parse_ical_feed,
    parse_master_schedule,
    parse_trip_pairing_packet,
    reconcile_feed_to_packet,
)
from nac_pay.schedule import (
    AppliedEventKind,
    Day,
    DutyType,
    Month,
    PilotProfile,
    Position,
    PremiumCategory,
    ReasonCode,
    Trip,
    apply_actuals_to_month,
    lower_month,
    month_from_master_schedule,
)

D = Decimal
DOCS = Path(__file__).resolve().parents[2] / "docs"
JUNE_FA = DOCS / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf"
JUNE_PACKET = DOCS / "JUNE 2026 Trip Pairing Packet.pdf"
ICAL = DOCS / "iCal_schedule_feed.ics"


# ── Reserve-designator + specificity in baseline↔packet matching ─────────


def test_flying_segments_strips_trailing_reserve_designator():
    from nac_pay.schedule.apply_actuals import _flying_segments

    assert _flying_segments("768/R1") == ("768",)
    assert _flying_segments("722/750") == ("722", "750")
    assert _flying_segments("R1") == ()          # pure reserve → matches nothing
    assert _flying_segments("720/1780") == ("720", "1780")


def test_match_reserve_designator_aid_to_packet_trip():
    """``768/R1`` (fly 768, then reserve) must reconcile to packet
    ``768/769`` instead of being mistaken for an open-time pickup."""
    from nac_pay.schedule.apply_actuals import (
        _find_baseline_aid_for_packet_trip,
        _flying_segments,
    )

    segs = [(a, _flying_segments(a)) for a in ("768/R1",)]
    assert _find_baseline_aid_for_packet_trip("768/769", segs) == "768/R1"


def test_longest_subsequence_match_wins():
    """When both a reserve-tail aid and a fuller aid could match, the more
    specific (longer) one claims the packet trip."""
    from nac_pay.schedule.apply_actuals import (
        _find_baseline_aid_for_packet_trip,
        _flying_segments,
    )

    segs = [(a, _flying_segments(a)) for a in ("722/R1", "722/750")]
    assert _find_baseline_aid_for_packet_trip("722/723/750/751", segs) == "722/750"


# ── Test helpers ────────────────────────────────────────────────────────


def _pilot(rate: str = "124.59") -> PilotProfile:
    return PilotProfile(
        pilot_id="DFI",
        name="FISHER",
        position=Position.FO,
        hourly_rate=D(rate),
    )


def _empty_month(line: str = "65", trips=(), days=()) -> Month:
    return Month(
        pilot=_pilot(),
        year=2026,
        month=6,
        line_value=D(line),
        trips=trips,
        days=days,
    )


def _trip_pairing(
    trip_id: str,
    pch: str,
    block: str = "4.17",
    duty: str = "7.0833",
) -> TripPairing:
    return TripPairing(
        trip_id=trip_id,
        raw_trip_id=trip_id + "//////",
        start_day_of_week="Wednesday",
        end_day_of_week="Wednesday",
        sch_block_hours=D(block),
        duty_hours=D(duty),
        tafb_hours=D(duty),
        total_dh_hours=D("0"),
        dpg_pch=D("3.82"),
        workdays=1,
        flight_op_pch=D(block),
        duty_rig_pch=D(duty) / D("2"),
        trip_rig_pch=D(duty) / D("4.90"),
        cumulative_dpg_pch=D("3.82"),
        deadhead_pch=D("0"),
        trip_pch_value=D(pch),
        dh_plus_trip_pch=D(pch),
        page_index=0,
    )


def _leg(
    flight_short: str,
    start: datetime,
    end: datetime,
    org: str = "ANC",
    dst: str = "BRW",
) -> FlightLegEvent:
    return FlightLegEvent(
        uid=f"leg-{flight_short}-{start.isoformat()}",
        dt_start_utc=start,
        dt_end_utc=end,
        flight_no_raw=f"NC{flight_short}",
        flight_no_short=flight_short,
        origin=org,
        destination=dst,
        tail="N000XX",
        customer="Test",
        captain="",
        first_officer="Dennis FISHER",
    )


def _matched_trip(
    trip_id: str,
    *,
    actual_block: str | None = None,
    packet_pch: str = "4.17",
    packet_block: str = "4.17",
    packet_duty: str = "7.0833",
    on_date: date = date(2026, 6, 12),
    legs_count: int = 3,
) -> ReconciledTrip:
    packet = _trip_pairing(trip_id, packet_pch, packet_block, packet_duty)
    # Synthetic legs that sum to actual_block (if provided) or to packet_block.
    target_block_hours = D(actual_block) if actual_block else D(packet_block)
    start_utc = datetime(on_date.year, on_date.month, on_date.day, 14, 30, tzinfo=timezone.utc)
    end_utc = start_utc + _hours_to_timedelta(target_block_hours)
    leg = _leg("768", start_utc, end_utc)
    return ReconciledTrip(
        flight_sequence=trip_id,
        legs=(leg,) * legs_count if legs_count > 1 else (leg,),
        packet_trip=packet,
        match_status=MatchStatus.MATCHED,
        first_dt_utc=start_utc,
        last_dt_utc=end_utc,
        actual_block_hours=target_block_hours,
    )


def _unmatched_trip(
    flight_sequence: str = "9999",
    on_date: date = date(2026, 6, 12),
    actual_block: str = "2.5",
    hour_utc: int = 14,
    minute_utc: int = 30,
) -> ReconciledTrip:
    block = D(actual_block)
    start = datetime(
        on_date.year, on_date.month, on_date.day, hour_utc, minute_utc,
        tzinfo=timezone.utc,
    )
    end = start + _hours_to_timedelta(block)
    leg = _leg(flight_sequence, start, end)
    return ReconciledTrip(
        flight_sequence=flight_sequence,
        legs=(leg,),
        packet_trip=None,
        match_status=MatchStatus.UNMATCHED_NO_PACKET,
        first_dt_utc=start,
        last_dt_utc=end,
        actual_block_hours=block,
    )


def _hours_to_timedelta(hours: Decimal):
    from datetime import timedelta
    seconds = int(hours * Decimal("3600"))
    return timedelta(seconds=seconds)


# ── Duty extension ──────────────────────────────────────────────────────


def test_duty_extension_adds_version_when_block_extends():
    """Baseline FLT 766 (pch 4.17, block 4.17h, duty 7.08h). iCal shows
    actual block extended to 5.00h. Recomputed Duty Rig from longer span
    pushes PCH up — version added, effective_pch reflects the uplift."""
    baseline_trip = Trip(
        trip_id="766",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))

    # Reconciled trip with actual block 5.00h (vs published 4.17h).
    # Duty span derived from first_dt_utc to last_dt_utc; for a single-leg
    # synthetic trip we set the leg span to the new block.
    rt = _matched_trip(
        "766",
        actual_block="5.00",
        packet_pch="4.17",
        packet_block="4.17",
        packet_duty="7.0833",
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    assert len(updated.trips) == 1
    updated_trip = updated.trips[0]
    assert len(updated_trip.versions) == 1
    assert updated_trip.effective_pch > D("4.17")

    duty_events = [e for e in events if e.kind is AppliedEventKind.DUTY_EXTENSION]
    assert len(duty_events) == 1
    assert duty_events[0].trip_id == "766"
    assert duty_events[0].delta_pch > 0


def test_no_event_when_actual_block_matches_packet():
    """Common case — pilot flew the trip as scheduled. No version added,
    no event logged."""
    baseline_trip = Trip(
        trip_id="766",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip("766", actual_block="4.17")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    assert updated.trips[0].versions == ()
    assert all(e.kind is not AppliedEventKind.DUTY_EXTENSION for e in events)


def test_sub_tolerance_extension_does_not_trigger():
    """A 2-minute (0.033h) overrun shouldn't bump anything — below the
    3-minute default tolerance."""
    baseline_trip = Trip(
        trip_id="766",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip("766", actual_block="4.20")    # +0.03h over 4.17
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)
    assert updated.trips[0].versions == ()


# ── Reserve callout ─────────────────────────────────────────────────────


def test_reserve_callout_sets_callout_trip_pch():
    """Baseline RSV on June 12, iCal flies trip 766 on June 12 → callout.
    Day.callout_trip_pch set to the matched trip's published PCH."""
    callout_date = date(2026, 6, 12)
    rsv_day = Day(
        date=callout_date,
        duty_type=DutyType.RSV,
        pch_value=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        label="1021",
    )
    baseline = _empty_month(days=(rsv_day,))

    rt = _matched_trip("766", actual_block="4.17", packet_pch="4.50", on_date=callout_date)
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    assert len(updated.days) == 1
    assert updated.days[0].date == callout_date
    assert updated.days[0].callout_trip_pch == D("4.50")
    # The flown trip id is captured too, so the calendar can surface it as the
    # bold "new" assignment over the subtle reserve line.
    assert updated.days[0].callout_trip_id == "766"
    callout_events = [e for e in events if e.kind is AppliedEventKind.RESERVE_CALLOUT]
    assert len(callout_events) == 1
    # delta_pch should be the excess over DPG = 4.50 - 3.82 = 0.68
    assert callout_events[0].delta_pch == D("0.68")


def test_reserve_callout_through_engine_matches_worked_check():
    """End-to-end via lowering + engine: 16 reserve days + 1 callout day
    on a 64.94 line should produce the same 65.68 PCH as the §6 worked
    check (test_reserve_callout_top_up_persists) — but driven by the
    apply_actuals path."""
    # 16 plain reserves + 1 RSV day that will receive a callout
    plain_reserves = tuple(
        Day(
            date=date(2026, 6, d),
            duty_type=DutyType.RSV,
            pch_value=D("3.82"),
            reason_code=ReasonCode.FLOWN,
            workdays=1,
            label=f"RSV-{d}",
        )
        for d in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)
    )
    callout_day = Day(
        date=date(2026, 6, 5),
        duty_type=DutyType.RSV,
        pch_value=D("3.82"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        label="RSV-5",
    )
    baseline = Month(
        pilot=_pilot(),
        year=2026,
        month=6,
        line_value=D("64.94"),
        days=plain_reserves + (callout_day,),
    )
    rt = _matched_trip(
        "X-CALLOUT",
        actual_block="4.50",
        packet_pch="4.50",
        on_date=date(2026, 6, 5),
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, _, _ = apply_actuals_to_month(baseline, reconciliation)
    result = compute_pay(lower_month(updated))

    assert result.option1_floor == D("65.68")     # 65 floor + 0.68 excess
    assert result.option3_earned == D("65.62")    # 16×3.82 + 4.50
    assert result.base_monthly_pch == D("65.68")


# ── Open-time pickup ────────────────────────────────────────────────────


def test_open_time_pickup_adds_new_trip_at_bid_period_default():
    """Reconciled trip with no baseline trip and no baseline RSV on the
    date → treated as a pickup. Defaults to OPEN_TIME_BID_PERIOD (1.0×) —
    safer than auto-promoting to 1.5× (pilot can promote in the GUI)."""
    baseline = _empty_month()   # no trips, no days
    rt = _matched_trip("999", packet_pch="3.82")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    assert len(updated.trips) == 1
    new_trip = updated.trips[0]
    assert new_trip.trip_id == "999"
    assert new_trip.published_pch == D("3.82")
    assert new_trip.premium_category is PremiumCategory.OPEN_TIME_BID_PERIOD
    assert new_trip.reason_code is ReasonCode.FLOWN

    pickup_events = [e for e in events if e.kind is AppliedEventKind.OPEN_TIME_PICKUP]
    assert len(pickup_events) == 1


# ── Duplicate-aid disambiguation via Trip.dates ────────────────────────


def test_same_aid_on_different_dates_disambiguates_by_date():
    """FISHER has aid='722/754' scheduled on TWO dates in a month. A duty
    extension on one of those dates must update *that* baseline Trip, not
    the first one with matching aid. Without Trip.dates this regressed —
    the duty extension landed on the wrong baseline slot."""
    trip_june_6 = Trip(
        trip_id="722/754",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 6),),
        label="722/754 on 2026-06-06",
    )
    trip_june_17 = Trip(
        trip_id="722/754",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 17),),
        label="722/754 on 2026-06-17",
    )
    baseline = _empty_month(trips=(trip_june_6, trip_june_17))

    # iCal trip on June 17 with extended block → should match the June-17 Trip
    rt = _matched_trip(
        "722/723/754/755",
        actual_block="6.50",      # > 5.25 + tolerance → triggers extension
        packet_pch="5.25",
        packet_block="5.25",
        packet_duty="9.15",
        on_date=date(2026, 6, 17),
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    # Both Trips survive; only the June-17 one has a version.
    assert len(updated.trips) == 2
    june_6_updated = next(t for t in updated.trips if date(2026, 6, 6) in t.dates)
    june_17_updated = next(t for t in updated.trips if date(2026, 6, 17) in t.dates)
    assert june_6_updated.versions == ()
    assert len(june_17_updated.versions) == 1

    duty_events = [e for e in events if e.kind is AppliedEventKind.DUTY_EXTENSION]
    assert len(duty_events) == 1
    assert duty_events[0].date == date(2026, 6, 17)


def test_falls_back_to_first_available_when_no_baseline_dates():
    """Synthetic / legacy Trips without dates fall back to first-available
    matching (the pre-dates behavior). This guards against accidentally
    breaking older Trip constructions that don't supply dates."""
    trip_a = Trip(
        trip_id="722/754",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    trip_b = Trip(
        trip_id="722/754",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(trip_a, trip_b))

    rt = _matched_trip(
        "722/723/754/755",
        actual_block="6.50",
        packet_pch="5.25",
        packet_block="5.25",
        packet_duty="9.15",
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))
    updated, _, _ = apply_actuals_to_month(baseline, reconciliation)

    # First trip gets the version; second is untouched.
    assert len(updated.trips[0].versions) == 1
    assert updated.trips[1].versions == ()


def test_pickup_of_same_pairing_not_swallowed_by_other_dated_day():
    """Regression — the real July 16 2026 pickup. FISHER's FA carries
    ``722/R1`` only on July 2 (a sick day); on June 26 the pilot picked up
    the same pairing for July 16 from open time, so July 16 is EMPTY on
    the FA. The flown July 16 trip matches packet ``722/723/R1`` and maps
    back to aid ``722/R1`` — but the only baseline candidate is dated
    July 2. The old first-available fallback let the July 2 trip claim
    it, so the pickup was credited NOWHERE (July total short 5.38 PCH).
    A dated candidate on a different date must be skipped → the trip
    flows to the open-time-pickup path."""
    trip_july_2 = Trip(
        trip_id="722/R1",
        published_pch=D("5.38"),
        reason_code=ReasonCode.SICK,
        workdays=1,
        dates=(date(2026, 7, 2),),
        label="722/R1 on 2026-07-02",
    )
    baseline = _empty_month(trips=(trip_july_2,))

    rt = _matched_trip(
        "722/723/R1",
        packet_pch="5.38",
        packet_block="2.92",
        packet_duty="10.75",
        on_date=date(2026, 7, 16),
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    # July 2 baseline untouched; July 16 credited as a pickup.
    assert len(updated.trips) == 2
    july_2 = next(t for t in updated.trips if date(2026, 7, 2) in t.dates)
    assert july_2.versions == ()
    assert july_2.reason_code is ReasonCode.SICK
    pickup = next(t for t in updated.trips if date(2026, 7, 16) in t.dates)
    assert pickup.trip_id == "722/723/R1"
    assert pickup.published_pch == D("5.38")
    assert pickup.premium_category is PremiumCategory.OPEN_TIME_BID_PERIOD

    pickup_events = [e for e in events if e.kind is AppliedEventKind.OPEN_TIME_PICKUP]
    assert len(pickup_events) == 1
    assert pickup_events[0].date == date(2026, 7, 16)


def test_dated_same_day_match_still_wins_alongside_pickup():
    """The date-preference path is unchanged: a flown trip whose date IS a
    scheduled occurrence still matches that baseline Trip (duty-extension
    path), even with the stricter no-cross-date rule in place."""
    trip_june_17 = Trip(
        trip_id="722/754",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 6, 17),),
        label="722/754 on 2026-06-17",
    )
    baseline = _empty_month(trips=(trip_june_17,))

    rt = _matched_trip(
        "722/723/754/755",
        actual_block="6.50",      # > 5.25 + tolerance → extension
        packet_pch="5.25",
        packet_block="5.25",
        packet_duty="9.15",
        on_date=date(2026, 6, 17),
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))
    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)

    assert len(updated.trips) == 1
    assert len(updated.trips[0].versions) == 1
    assert not [e for e in events if e.kind is AppliedEventKind.OPEN_TIME_PICKUP]


# ── Feed cancellations (LEA OFF/PAY PROTECTED) ────────────────────────


def _off_event(label: str, on_date: date):
    """An all-day BlueOne LEA event: DTSTART 08:00Z = local midnight AKDT."""
    from nac_pay.parsers import OffEvent
    start = datetime(on_date.year, on_date.month, on_date.day, 8, 0,
                     tzinfo=timezone.utc)
    return OffEvent(
        uid="test-lea-1",
        dt_start_utc=start,
        dt_end_utc=start + _hours_to_timedelta(D("23.98")),
        label=label,
    )


def test_pay_protected_lea_marks_scheduled_trip_cancelled():
    """The real July 15 2026 scenario: the feed removed 768/R1's legs and
    posted ``LEA - OFF/PAY PROTECTED`` in their place. The scheduled trip
    is stamped cancelled_pay_protected (display) with the published PCH
    untouched (a company action never reduces pay), and a
    COMPANY_CANCELLATION event is logged."""
    from nac_pay.schedule import apply_feed_cancellations

    trip = Trip(
        trip_id="768/R1",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 7, 15),),
    )
    baseline = _empty_month(trips=(trip,))
    off = _off_event("OFF/PAY PROTECTED", date(2026, 7, 15))

    updated, events = apply_feed_cancellations(baseline, (off,))

    assert len(updated.trips) == 1
    marked = updated.trips[0]
    assert marked.cancelled_pay_protected is True
    assert marked.published_pch == D("5.25")
    assert marked.effective_pch == D("5.25")
    assert marked.reason_code is ReasonCode.FLOWN

    assert len(events) == 1
    ev = events[0]
    assert ev.kind is AppliedEventKind.COMPANY_CANCELLATION
    assert ev.date == date(2026, 7, 15)
    assert ev.trip_id == "768/R1"
    assert ev.delta_pch == D("0")


def test_plain_lea_off_does_not_cancel():
    """Ordinary ``LEA - OFF`` / ``LEA - SICK`` day-status events are NOT a
    cancellation signal — only the explicit PAY PROTECTED label is."""
    from nac_pay.schedule import apply_feed_cancellations

    trip = Trip(
        trip_id="768/R1",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 7, 15),),
    )
    baseline = _empty_month(trips=(trip,))

    for label in ("OFF", "SICK", "TRIP DROP"):
        updated, events = apply_feed_cancellations(
            baseline, (_off_event(label, date(2026, 7, 15)),),
        )
        assert updated.trips[0].cancelled_pay_protected is False
        assert events == ()


def test_pay_protected_lea_on_unscheduled_day_is_noop():
    """A pay-protected LEA on a date with no scheduled trip changes nothing
    (nothing was cancelled — e.g. an already-empty day)."""
    from nac_pay.schedule import apply_feed_cancellations

    baseline = _empty_month()
    off = _off_event("OFF/PAY PROTECTED", date(2026, 7, 15))

    updated, events = apply_feed_cancellations(baseline, (off,))
    assert updated is baseline
    assert events == ()


def test_pay_protected_label_match_is_case_insensitive():
    from nac_pay.schedule import apply_feed_cancellations

    trip = Trip(
        trip_id="768/R1",
        published_pch=D("5.25"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(date(2026, 7, 15),),
    )
    baseline = _empty_month(trips=(trip,))
    off = _off_event("Off/Pay Protected", date(2026, 7, 15))

    updated, events = apply_feed_cancellations(baseline, (off,))
    assert updated.trips[0].cancelled_pay_protected is True
    assert len(events) == 1


# ── Unmatched ──────────────────────────────────────────────────────────


def test_unmatched_trip_never_added_silently():
    """An unmatched reconciled trip (no packet match) must never be added
    SILENTLY: on an off day it becomes a gated, confirmable pickup proposal
    (see the off-day pickup tests below) — always paired with a
    FeedReassignment record the pilot can reject — never a bare Trip."""
    baseline = _empty_month()
    rt = _unmatched_trip(flight_sequence="9999/9998")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    added = [t for t in updated.trips if t.trip_id == "9999/9998"]
    assert len(added) == 1
    gating = [r for r in reassigns if r.signature == "9999/9998"]
    assert len(gating) == 1
    assert gating[0].kind == "OFF_DAY_PICKUP"
    assert gating[0].status == "PROPOSED"       # pilot must confirm/reject


# ── Feed-detected company reassignment (reroute) ────────────────────────


def _scheduled_trip(trip_id="730/732", pch="4.50", on=date(2026, 6, 12)) -> Trip:
    return Trip(
        trip_id=trip_id,
        published_pch=D(pch),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
        dates=(on,),
    )


def test_feed_reassignment_applied_on_scheduled_day_pays_greater():
    """An unmatched feed trip (company reroute) landing on a day that already
    carries an FA-scheduled trip becomes a reassignment: a version is
    attached and the day pays max(original, recomputed) — protected, never
    below published. Default (no decision) = PROPOSED."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on)      # not in packet
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    trip = updated.trips[0]
    assert len(trip.versions) == 1
    assert trip.effective_pch >= D("4.50")               # protected floor

    assert len(reassigns) == 1
    fr = reassigns[0]
    assert fr.signature == "730/730/731"
    assert fr.original_aid == "730/732"
    assert fr.original_pch == D("4.50")
    assert fr.status == "PROPOSED"
    assert fr.applied is True
    assert fr.effective_pch == trip.effective_pch
    assert fr.effective_pch == max(fr.original_pch, fr.new_pch)

    ev = [e for e in events if e.kind is AppliedEventKind.FEED_REASSIGNMENT]
    assert len(ev) == 1
    assert ev[0].trip_id == "730/730/731"
    # It's a reassignment, NOT a bare unmatched-review log.
    assert all(e.kind is not AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)


def test_feed_reassignment_recompute_can_exceed_published():
    """When the reroute's recomputed PCH beats the published value, the day
    pays the recompute (uplift)."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "3.00", on),))
    rt = _unmatched_trip("730/730/731", on_date=on, actual_block="5.00")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    fr = reassigns[0]
    assert fr.new_pch >= D("5.00")                        # flight-op = actual block
    assert fr.effective_pch == fr.new_pch                 # beats published 3.00
    assert updated.trips[0].effective_pch == fr.new_pch


def test_feed_reassignment_borrows_tafb_from_original_packet():
    """The reroute isn't in the packet, so trip-rig borrows the ORIGINAL
    trip's TAFB from the packet (passed in). Here a large original TAFB makes
    trip-rig the winning §3.E component (49.0h ÷ 4.90 = 10.00)."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on, actual_block="2.5")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    packet = {"730/732": _trip_pairing("730/732", "4.50", block="4.17", duty="49.0")}

    _updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation, packet=packet,
    )

    assert reassigns[0].new_pch == D("10.00")            # 49.0 / 4.90 trip-rig
    assert reassigns[0].effective_pch == D("10.00")


def test_feed_reassignment_pch_override_pays_company_value():
    """A pilot-entered company PCH (the company sometimes assigns a value the
    feed can't express) replaces the recomputed value — paid as max(published,
    override). Here 5.17 beats published 4.50 and the recomputed 3.82."""
    on = date(2026, 7, 6)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("732/732/733", on_date=on, actual_block="2.5")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {("2026-07-06", "732/732/733"): "CONFIRMED"}
    overrides = {("2026-07-06", "732/732/733"): D("5.17")}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions=decisions,
        feed_reassignment_pch_overrides=overrides,
    )

    fr = reassigns[0]
    assert fr.override_pch == D("5.17")
    assert fr.effective_pch == D("5.17")                 # company value wins
    assert updated.trips[0].effective_pch == D("5.17")
    # The attached version carries the credited (override) value.
    assert updated.trips[0].versions[-1].pch_value == D("5.17")


def test_feed_reassignment_pch_override_still_protected_by_published():
    """Pay protection holds: an override below the published value never
    reduces pay — the day still pays the published floor."""
    on = date(2026, 7, 6)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("732/732/733", on_date=on, actual_block="2.5")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {("2026-07-06", "732/732/733"): "CONFIRMED"}
    overrides = {("2026-07-06", "732/732/733"): D("3.00")}  # below published 4.50

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions=decisions,
        feed_reassignment_pch_overrides=overrides,
    )

    assert reassigns[0].override_pch == D("3.00")
    assert reassigns[0].effective_pch == D("4.50")       # protected floor
    assert updated.trips[0].effective_pch == D("4.50")


def test_feed_reassignment_rejected_reverts_to_fa_original():
    """A REJECTED decision suppresses the reassignment: no version is
    attached, the day pays the FA original, and applied is False."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on)
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {("2026-06-12", "730/730/731"): "REJECTED"}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation, feed_reassignment_decisions=decisions,
    )

    assert updated.trips[0].versions == ()
    assert updated.trips[0].effective_pch == D("4.50")
    fr = reassigns[0]
    assert fr.status == "REJECTED"
    assert fr.applied is False
    assert fr.effective_pch == D("4.50")


def test_feed_reassignment_confirmed_status_still_applies():
    """A CONFIRMED decision keeps the reassignment applied and marks it
    confirmed (clears the calendar's confirm badge)."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on)
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {("2026-06-12", "730/730/731"): "CONFIRMED"}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation, feed_reassignment_decisions=decisions,
    )

    assert len(updated.trips[0].versions) == 1
    assert reassigns[0].status == "CONFIRMED"
    assert reassigns[0].applied is True


def test_unmatched_trip_on_unscheduled_day_is_offday_pickup_not_reroute():
    """An unmatched feed trip on a day with NO scheduled trip must NOT be
    treated as a reroute of a scheduled trip on a *different* date — the
    scheduled trip is left untouched and the trip surfaces as an off-day
    pickup proposal on its own date."""
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", date(2026, 6, 10)),))
    rt = _unmatched_trip("8888", on_date=date(2026, 6, 12))     # different day
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    assert updated.trips[0].versions == ()                       # untouched
    assert len(reassigns) == 1
    assert reassigns[0].kind == "OFF_DAY_PICKUP"
    assert reassigns[0].date == date(2026, 6, 12)
    assert all(e.kind is not AppliedEventKind.FEED_REASSIGNMENT for e in events)


def test_feed_reassignment_attributed_by_local_date_not_utc():
    """Regression (July 6 732/732/733): an evening reroute departs 02:00 UTC
    the *next* calendar day but 18:00 AKDT the *scheduled* day. apply_actuals
    must attribute it by Anchorage-local date, else it looks for the scheduled
    trip on the wrong (UTC) day, finds none, and silently drops to a log-only
    review instead of surfacing on the calendar/day.

    Scheduled trip is on July 6; the reroute departs 2026-07-07 02:00 UTC
    (== 2026-07-06 18:00 AKDT). It must land on July 6 as a reassignment."""
    scheduled_day = date(2026, 7, 6)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", scheduled_day),))
    # first_dt = 2026-07-07 02:00 UTC → local July 6; UTC .date() would be July 7.
    rt = _unmatched_trip(
        "732/732/733", on_date=date(2026, 7, 7), hour_utc=2, minute_utc=0,
    )
    assert rt.first_dt_utc.date() == date(2026, 7, 7)         # UTC day (the trap)
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    # Surfaces as a reassignment on the LOCAL scheduled day, not a review item.
    assert len(reassigns) == 1
    assert reassigns[0].date == scheduled_day                # July 6, not July 7
    assert reassigns[0].signature == "732/732/733"
    assert reassigns[0].original_aid == "730/732"
    assert len(updated.trips[0].versions) == 1
    fe = [e for e in events if e.kind is AppliedEventKind.FEED_REASSIGNMENT]
    assert len(fe) == 1 and fe[0].date == scheduled_day
    assert all(e.kind is not AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)


# ── End-to-end integration: real June data ─────────────────────────────


def test_integration_june_baseline_with_ical_actuals_runs_through_engine():
    """Full pipeline against real June 2026 inputs:
    FA → baseline Month → reconcile iCal × packet → apply actuals → engine.

    The iCal sample covers June 7-29 — within that window FISHER's actual
    flights match her FA schedule exactly (June 12 aid='768' → iCal trip
    '768/768/769'; June 17 aid='722/754' → iCal trip '722/723/754/755').
    No duty extensions, no callouts, no pickups, no unmatched. The
    apply_actuals layer should be a no-op: trip + day counts unchanged,
    pay equals the baseline line value × rate exactly.
    """
    fa_grids = parse_master_schedule(str(JUNE_FA))
    pilot = _pilot()
    baseline, _warnings = month_from_master_schedule(fa_grids["DFI"], pilot)

    feed = parse_ical_feed(str(ICAL))
    packet = parse_trip_pairing_packet(str(JUNE_PACKET))
    reconciliation = reconcile_feed_to_packet(feed, packet)
    updated, applied, _ = apply_actuals_to_month(baseline, reconciliation)

    # Apply was a no-op: 7 baseline trips + 8 baseline RSV days preserved
    # exactly. No events fired.
    assert len(updated.trips) == len(baseline.trips) == 7
    assert len(updated.days) == len(baseline.days) == 8
    assert applied == ()

    result = compute_pay(lower_month(updated))
    assert result.option3_earned == D("65.78")
    assert result.base_monthly_pch == D("65.78")
    assert result.topup_pch == D("0.00")
    # 65.78 × $124.59 = $8195.5302 → $8195.53
    assert result.total_pay == D("8195.53")


# ── Auto duty-rig credit: padded duty + callout recompute ──────────────


def _rt_with_span(trip_id, *, packet_pch, packet_block, packet_duty,
                  actual_block, span_hours, on_date=date(2026, 6, 12)):
    """A matched ReconciledTrip whose duty SPAN (first out → last in) is set
    independently of block — to exercise a long-duty / normal-block case."""
    packet = _trip_pairing(trip_id, packet_pch, packet_block, packet_duty)
    start = datetime(on_date.year, on_date.month, on_date.day, 14, 0, tzinfo=timezone.utc)
    end = start + _hours_to_timedelta(D(span_hours))
    leg = _leg("768", start, end)
    return ReconciledTrip(
        flight_sequence=trip_id, legs=(leg,), packet_trip=packet,
        match_status=MatchStatus.MATCHED, first_dt_utc=start, last_dt_utc=end,
        actual_block_hours=D(actual_block),
    )


def test_duty_extension_triggers_on_long_duty_not_just_block():
    """Block flown as scheduled (4.17h) but the duty SPAN is 13h (long ground
    time). Padded duty 14.25h → rig 7.125 beats published 4.17 → version added,
    even though block did not extend (the old block-only gate missed this)."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17", span_hours="13.0")
    updated, events, _ = apply_actuals_to_month(
        baseline, ReconciliationResult(trips=(rt,), matched=(rt,)))
    trip = updated.trips[0]
    assert len(trip.versions) == 1
    assert trip.effective_pch == D("7.125")    # (13 + 1.25)/2
    assert any(e.kind is AppliedEventKind.DUTY_EXTENSION for e in events)


def test_callout_auto_credits_actual_recompute():
    """A long callout auto-credits the §3.E recompute from actuals, not just
    the published value. Duty span 12h → padded 13.25h → rig 6.625 > published
    4.50 → callout_trip_pch = 6.625."""
    callout_date = date(2026, 6, 12)
    rsv = Day(date=callout_date, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="RSV")
    baseline = _empty_month(days=(rsv,))
    rt = _rt_with_span("766", packet_pch="4.50", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="12.0", on_date=callout_date)
    updated, events, _ = apply_actuals_to_month(
        baseline, ReconciliationResult(trips=(rt,), matched=(rt,)))
    assert updated.days[0].callout_trip_pch == D("6.625")   # (12 + 1.25)/2 credited
    assert updated.days[0].callout_published_pch == D("4.50")  # true published kept
    assert updated.days[0].callout_trip_id == "766"


def test_callout_keeps_published_when_actuals_dont_beat_it():
    """Short callout: published 4.50 stands when the actual recompute (4.17)
    is below it — no spurious inflation."""
    callout_date = date(2026, 6, 12)
    rsv = Day(date=callout_date, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="RSV")
    baseline = _empty_month(days=(rsv,))
    rt = _rt_with_span("766", packet_pch="4.50", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="4.17", on_date=callout_date)
    updated, _, _ = apply_actuals_to_month(
        baseline, ReconciliationResult(trips=(rt,), matched=(rt,)))
    assert updated.days[0].callout_trip_pch == D("4.50")


# ── packet_trip_for_aid: resolve packet by FA aid (feed-independent) ────


def test_packet_trip_for_aid_subsequence_match():
    from nac_pay.schedule.apply_actuals import packet_trip_for_aid

    packet = {
        "768/768/769": _trip_pairing("768/768/769", "4.17"),
        "720/721/1780/1781": _trip_pairing("720/721/1780/1781", "8.00"),
    }
    # Short FA aid resolves to its full packet sequence by subsequence.
    assert packet_trip_for_aid("768", packet).trip_id == "768/768/769"
    # Exact key wins.
    assert packet_trip_for_aid("720/721/1780/1781", packet).trip_id == "720/721/1780/1781"
    # A flown subset (e.g. legs survived, others aged out) still matches.
    assert packet_trip_for_aid("721/1780/1781", packet).trip_id == "720/721/1780/1781"
    # A reserve designator tail is stripped before matching.
    assert packet_trip_for_aid("768/R1", packet).trip_id == "768/768/769"
    # A bare reserve line (no flying segments) matches nothing.
    assert packet_trip_for_aid("1021", packet) is None
    # An unknown trip matches nothing.
    assert packet_trip_for_aid("999", packet) is None


# ── Off-day pickups (company-added trip on a day with no scheduled flying) ──


def test_offday_pickup_surfaces_as_proposal_with_dpg_floor():
    """2026-07-23 incident: company adds 2720/2721 on an OFF day. Must become
    a FeedReassignment proposal (kind OFF_DAY_PICKUP) + a pickup Trip paying
    the recompute — block 2.57 loses to the 3.82 DPG floor."""
    on = date(2026, 6, 12)
    baseline = _empty_month()                       # no trips, no RSV days
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)

    assert len(reassigns) == 1
    fr = reassigns[0]
    assert fr.kind == "OFF_DAY_PICKUP"
    assert fr.signature == "2720/2721"
    assert fr.original_aid == "OFF"
    assert fr.original_pch == D("0")
    assert fr.new_pch == D("3.82")
    assert fr.effective_pch == D("3.82")
    assert fr.status == "PROPOSED"
    assert fr.applied is True

    added = updated.trips[-1]
    assert added.trip_id == "2720/2721"
    assert added.published_pch == D("3.82")
    assert added.premium_category is PremiumCategory.OPEN_TIME_BID_PERIOD
    assert added.dates == (on,)

    assert any(e.kind is AppliedEventKind.OFF_DAY_PICKUP for e in events)
    assert all(e.kind is not AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)


def test_offday_pickup_rejected_adds_nothing():
    on = date(2026, 6, 12)
    baseline = _empty_month()
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions={(on.isoformat(), "2720/2721"): "REJECTED"},
    )
    fr = reassigns[0]
    assert fr.status == "REJECTED" and fr.applied is False
    assert fr.effective_pch == D("0")
    assert all(t.trip_id != "2720/2721" for t in updated.trips)


def test_offday_pickup_company_pch_override_wins():
    on = date(2026, 6, 12)
    baseline = _empty_month()
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="2.57")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions={(on.isoformat(), "2720/2721"): "CONFIRMED"},
        feed_reassignment_pch_overrides={(on.isoformat(), "2720/2721"): D("4.50")},
    )
    fr = reassigns[0]
    assert fr.status == "CONFIRMED" and fr.override_pch == D("4.50")
    assert fr.effective_pch == D("4.50")
    assert updated.trips[-1].published_pch == D("4.50")


def test_unmatched_on_rsv_day_stays_review_only():
    """Reserve days keep the current behavior — the callout flow owns them."""
    on = date(2026, 6, 12)
    rsv = Day(date=on, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="RSV")
    baseline = _empty_month(days=(rsv,))
    rt = _unmatched_trip("2720/2721", on_date=on)
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    _updated, events, reassigns = apply_actuals_to_month(baseline, reconciliation)
    assert reassigns == ()
    assert any(e.kind is AppliedEventKind.UNMATCHED_TRIP_REVIEW for e in events)
