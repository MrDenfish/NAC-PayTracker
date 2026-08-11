"""Tests for ``schedule.apply_actuals_to_month``.

Synthetic unit tests per event kind, plus one end-to-end integration test
against real June 2026 data (Final Award + Trip Pairing Packet + iCal feed).
"""

from __future__ import annotations

from dataclasses import replace
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


def test_sub_block_tolerance_extension_does_not_trigger():
    """A sub-minute (0.005h) block diff is float noise, not a real overrun —
    it stays below the tight block tolerance and doesn't churn a version."""
    baseline_trip = Trip(
        trip_id="766",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip("766", actual_block="4.175")   # +0.005h — sub-minute noise
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)
    assert updated.trips[0].versions == ()


def test_small_block_overrun_within_old_duty_tolerance_credits():
    """The Aug 1 720/1780 case: actual BLOCK 6.12 vs published 6.08 — a real
    ~2.4-min flight-op overrun that the old 0.05 duty tolerance swallowed
    (effective stuck at 6.08). Block is directly measured, so it credits past
    the tight block tolerance: effective_pch must be 6.12."""
    baseline_trip = Trip(
        trip_id="720/1780",
        published_pch=D("6.08"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip(
        "720/1780", packet_pch="6.08", packet_block="6.08",
        packet_duty="11.42", actual_block="6.12",
    )
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, events, _ = apply_actuals_to_month(baseline, reconciliation)
    trip = updated.trips[0]
    assert trip.effective_pch == D("6.12")
    assert any(e.kind is AppliedEventKind.DUTY_EXTENSION for e in events)


def test_actual_duty_starts_at_the_packet_report_time_not_actual_blockout():
    """Duty starts when the pilot reports, and the pilot reports on the
    published schedule — a late push does not shorten the duty day.

    Aug 8 2026: packet show 04:41 (1:00 before the scheduled 05:41
    departure), flight actually pushed at 06:00 local. Anchoring on the
    actual block-out gave duty-on 05:00 and swallowed the 19-minute delay,
    understating duty by 0.32h and the duty rig by 0.16."""
    from nac_pay.schedule.apply_actuals import _actual_duty_hours

    packet = _trip_pairing("720/721/1780/1781", "6.08")
    packet = replace(packet, sched_duty_on="04:41")
    # 14:00Z = 06:00 AKDT (the delayed push); 02:00Z next day = 18:00 AKDT.
    start = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)
    rt = ReconciledTrip(
        flight_sequence="720/721/1780/1781/1781",
        legs=(_leg("720", start, end),), packet_trip=packet,
        match_status=MatchStatus.MATCHED,
        first_dt_utc=start, last_dt_utc=end, actual_block_hours=D("7.13"),
    )

    # 04:41 → 18:15 (last in + 0:15) = 13:34 = 13.5667h, not 05:00 → 18:15.
    assert abs(_actual_duty_hours(rt) - D("13.5667")) < D("0.001")


def test_actual_duty_falls_back_to_blockout_pad_without_a_packet_show_time():
    """No packet trip (a reroute, an off-day pickup) or an unparsed show
    time leaves nothing to anchor to — keep the actual-out − 1:00 estimate
    rather than inventing a report time."""
    from nac_pay.schedule.apply_actuals import _actual_duty_hours

    start = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)
    rt = ReconciledTrip(
        flight_sequence="9999", legs=(_leg("9999", start, end),),
        packet_trip=None, match_status=MatchStatus.UNMATCHED_NO_PACKET,
        first_dt_utc=start, last_dt_utc=end, actual_block_hours=D("7.13"),
    )

    # 05:00 → 18:15 = 13.25h.
    assert abs(_actual_duty_hours(rt) - D("13.25")) < D("0.001")


def test_small_duty_rig_overrun_keeps_wider_tolerance():
    """The duty-rig path (built on estimated report/release padding) keeps the
    wider 0.05 tolerance — a duty-only overrun that clears the block tolerance
    but not the duty tolerance must NOT trigger. Block flown as scheduled
    (4.17); padded duty 8.42h → rig 4.21, only +0.04 over published 4.17."""
    baseline_trip = Trip(
        trip_id="766",
        published_pch=D("4.17"),
        reason_code=ReasonCode.FLOWN,
        workdays=1,
    )
    baseline = _empty_month(trips=(baseline_trip,))
    # span 7.17h + 1.25h pad = 8.42h duty → rig 4.21 (published + 0.04, < 0.05);
    # block held at 4.17 so only the sub-tolerance duty-rig could trigger.
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="7.17")
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


# ── End-to-end regression: the 2026-07-23 incident, both feed states ────


def test_july23_incident_transitional_and_final_feed_states():
    """Replay of the 2026-07-23 prod incident (dates as June).

    FA: 23rd OFF, 24th 768/R1 (5.25), 25th 720/721/1780/1781 (6.08).
    State A (transitional): feed still has 768/769 on the 24th evening AND
    the 25th's four legs (an 8.5h overnight gap chained them), plus the new
    2720/2721 extra-section on the 23rd. Bug was: six legs fused into one
    group attributed to the 24th → bogus 13.21 "reassignment"; 2720/2721
    invisible (log-only review).
    State B (final): 768/769 legs removed, LEA OFF/PAY PROTECTED on the 24th.
    """
    from nac_pay.parsers import OffEvent
    from nac_pay.schedule import apply_feed_cancellations

    utc = timezone.utc
    d23, d24 = date(2026, 6, 23), date(2026, 6, 24)
    baseline = _empty_month(trips=(
        _scheduled_trip("768/R1", "5.25", d24),
        _scheduled_trip("720/721/1780/1781", "6.08", date(2026, 6, 25)),
    ))
    packet = {
        "768/769/R1": _trip_pairing("768/769/R1", "5.25"),
        "720/721/1780/1781": _trip_pairing("720/721/1780/1781", "6.08"),
    }
    legs_23 = (   # 14:30–15:45, 16:20–17:39 ANC on the 23rd
        _leg("2720", datetime(2026, 6, 23, 22, 30, tzinfo=utc),
             datetime(2026, 6, 23, 23, 45, tzinfo=utc), org="ANC", dst="OME"),
        _leg("2721", datetime(2026, 6, 24, 0, 20, tzinfo=utc),
             datetime(2026, 6, 24, 1, 39, tzinfo=utc), org="OME", dst="ANC"),
    )
    legs_24 = (   # 18:00–19:15, 19:50–21:09 ANC on the 24th
        _leg("768", datetime(2026, 6, 25, 2, 0, tzinfo=utc),
             datetime(2026, 6, 25, 3, 15, tzinfo=utc), org="ANC", dst="OME"),
        _leg("769", datetime(2026, 6, 25, 3, 50, tzinfo=utc),
             datetime(2026, 6, 25, 5, 9, tzinfo=utc), org="OME", dst="ANC"),
    )
    legs_25 = (   # 05:41 → 15:10 ANC on the 25th
        _leg("720", datetime(2026, 6, 25, 13, 41, tzinfo=utc),
             datetime(2026, 6, 25, 15, 11, tzinfo=utc), org="ANC", dst="OME"),
        _leg("721", datetime(2026, 6, 25, 16, 1, tzinfo=utc),
             datetime(2026, 6, 25, 17, 26, tzinfo=utc), org="OME", dst="ANC"),
        _leg("1780", datetime(2026, 6, 25, 19, 0, tzinfo=utc),
             datetime(2026, 6, 25, 20, 35, tzinfo=utc), org="ANC", dst="DGG"),
        _leg("1781", datetime(2026, 6, 25, 21, 35, tzinfo=utc),
             datetime(2026, 6, 25, 23, 10, tzinfo=utc), org="DGG", dst="ANC"),
    )

    # ── State A: transitional feed ──────────────────────────────────────
    rec = reconcile_feed_to_packet(
        ParsedFeed(flight_legs=legs_23 + legs_24 + legs_25), packet,
    )
    updated, events, reassigns = apply_actuals_to_month(
        baseline, rec, packet=packet,
    )
    # No bogus reroute of the 24th: 768/769 matched its own packet trip.
    assert all(fr.kind != "REROUTE" for fr in reassigns)
    # The 23rd surfaces as an off-day pickup at the DPG floor.
    pickup = next(fr for fr in reassigns if fr.kind == "OFF_DAY_PICKUP")
    assert pickup.date == d23 and pickup.signature == "2720/2721"
    assert pickup.effective_pch == D("3.82")
    # The 24th and 25th keep their published values.
    by_id = {t.trip_id: t for t in updated.trips}
    assert by_id["768/R1"].effective_pch == D("5.25")
    assert by_id["720/721/1780/1781"].effective_pch == D("6.08")

    # ── State B: final feed (cancellation posted, 768/769 gone) ─────────
    rec_b = reconcile_feed_to_packet(
        ParsedFeed(flight_legs=legs_23 + legs_25), packet,
    )
    updated_b, _ev, reassigns_b = apply_actuals_to_month(
        baseline, rec_b, packet=packet,
    )
    cancel = OffEvent(
        uid="lea-1", label="OFF/PAY PROTECTED",
        dt_start_utc=datetime(2026, 6, 24, 8, 0, tzinfo=utc),
        dt_end_utc=datetime(2026, 6, 25, 7, 59, tzinfo=utc),
    )
    updated_b, cancel_events = apply_feed_cancellations(updated_b, (cancel,))
    by_id_b = {t.trip_id: t for t in updated_b.trips}
    assert by_id_b["768/R1"].cancelled_pay_protected is True
    assert by_id_b["768/R1"].effective_pch == D("5.25")
    assert by_id_b["720/721/1780/1781"].effective_pch == D("6.08")
    assert any(fr.kind == "OFF_DAY_PICKUP" and fr.date == d23 for fr in reassigns_b)
    assert len(cancel_events) == 1


# ── LEA day-status consumer: feed drops + sick seeding ──────────────────


def _off_event(label: str, on: date) -> "OffEvent":
    from nac_pay.parsers import OffEvent
    return OffEvent(
        uid=f"lea-{label}-{on.isoformat()}",
        label=label,
        dt_start_utc=datetime(on.year, on.month, on.day, 8, 0, tzinfo=timezone.utc),
        dt_end_utc=datetime(on.year, on.month, on.day + 1, 7, 59, tzinfo=timezone.utc),
    )


def test_detect_feed_drops_proposes_on_trip_drop_event():
    """LEA - TRIP DROP on a scheduled day => PROPOSED drop, no pay change
    (the month is never mutated by detection — published keeps paying)."""
    from nac_pay.schedule import detect_feed_drops

    on = date(2026, 6, 24)
    baseline = _empty_month(trips=(_scheduled_trip("768/R1", "5.25", on),))
    drops, events = detect_feed_drops(
        baseline, (_off_event("TRIP DROP", on),), rejected_dates=set(),
    )
    assert len(drops) == 1
    fd = drops[0]
    assert fd.date == on
    assert fd.original_aid == "768/R1"
    assert fd.published_pch == D("5.25")
    assert fd.status == "PROPOSED"
    assert any(e.kind is AppliedEventKind.FEED_DROP for e in events)


def test_detect_feed_drops_confirmed_when_trip_already_dropped():
    from nac_pay.schedule import detect_feed_drops
    from dataclasses import replace as _replace

    on = date(2026, 6, 24)
    dropped = _replace(
        _scheduled_trip("768/R1", "5.25", on),
        reason_code=ReasonCode.VOLUNTARY_DROP,
    )
    baseline = _empty_month(trips=(dropped,))
    drops, events = detect_feed_drops(
        baseline, (_off_event("TRIP DROP", on),), rejected_dates=set(),
    )
    assert drops[0].status == "CONFIRMED"
    assert not any(e.kind is AppliedEventKind.FEED_DROP for e in events)


def test_detect_feed_drops_rejected_is_silent():
    from nac_pay.schedule import detect_feed_drops

    on = date(2026, 6, 24)
    baseline = _empty_month(trips=(_scheduled_trip("768/R1", "5.25", on),))
    drops, events = detect_feed_drops(
        baseline, (_off_event("TRIP DROP", on),), rejected_dates={on.isoformat()},
    )
    assert drops[0].status == "REJECTED"
    assert events == ()


def test_detect_feed_drops_ignores_other_labels_and_tripless_dates():
    from nac_pay.schedule import detect_feed_drops

    on = date(2026, 6, 24)
    baseline = _empty_month(trips=(_scheduled_trip("768/R1", "5.25", on),))
    drops, _ = detect_feed_drops(
        baseline,
        (_off_event("OFF", on), _off_event("OFF/PAY PROTECTED", on),
         _off_event("TRIP DROP", date(2026, 6, 25))),   # no trip on the 25th
        rejected_dates=set(),
    )
    assert drops == ()


def test_lea_sick_seeds_flown_trip():
    from nac_pay.schedule import apply_lea_reason_seeds

    on = date(2026, 6, 2)
    baseline = _empty_month(trips=(_scheduled_trip("722/750", "4.92", on),))
    updated, events = apply_lea_reason_seeds(baseline, (_off_event("SICK", on),))
    assert updated.trips[0].reason_code is ReasonCode.SICK
    assert any(e.kind is AppliedEventKind.LEA_REASON_SEED for e in events)
    # Pay is untouched — SICK keeps published, protected.
    assert updated.trips[0].effective_pch == D("4.92")


def test_lea_sick_does_not_override_non_flown_reason():
    from nac_pay.schedule import apply_lea_reason_seeds
    from dataclasses import replace as _replace

    on = date(2026, 6, 2)
    pto = _replace(
        _scheduled_trip("722/750", "4.92", on), reason_code=ReasonCode.PTO,
    )
    baseline = _empty_month(trips=(pto,))
    updated, events = apply_lea_reason_seeds(baseline, (_off_event("SICK", on),))
    assert updated.trips[0].reason_code is ReasonCode.PTO
    assert events == ()


def test_lea_sick_seeds_paying_day_entry():
    from nac_pay.schedule import apply_lea_reason_seeds

    on = date(2026, 6, 16)
    rsv = Day(date=on, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="1021")
    baseline = _empty_month(days=(rsv,))
    updated, events = apply_lea_reason_seeds(baseline, (_off_event("SICK", on),))
    assert updated.days[0].reason_code is ReasonCode.SICK
    assert len(events) == 1


def test_lea_non_sick_labels_do_not_seed():
    from nac_pay.schedule import apply_lea_reason_seeds

    on = date(2026, 6, 2)
    baseline = _empty_month(trips=(_scheduled_trip("722/750", "4.92", on),))
    updated, events = apply_lea_reason_seeds(
        baseline,
        (_off_event("OFF", on), _off_event("TRIP DROP", on),
         _off_event("OFF/PAY PROTECTED", on)),
    )
    assert updated.trips[0].reason_code is ReasonCode.FLOWN
    assert events == ()


# ── Duty override (Task 4): replaces feed-derived duty in §3.E recompute ─


def test_duty_override_replaces_the_feed_derived_duty():
    from nac_pay.schedule.apply_actuals import _actual_duty_hours

    packet = _trip_pairing("720/721/1780/1781", "6.08")
    packet = replace(packet, sched_duty_on="04:41")
    start = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc)
    rt = ReconciledTrip(
        flight_sequence="720/721/1780/1781", legs=(_leg("720", start, end),),
        packet_trip=packet, match_status=MatchStatus.MATCHED,
        first_dt_utc=start, last_dt_utc=end, actual_block_hours=D("7.13"),
    )

    # No override → the packet-anchored window (PR #76): 04:41 → 18:15.
    assert abs(_actual_duty_hours(rt) - D("13.5667")) < D("0.001")
    # Override → exactly what the pilot said, in this case SHORTER.
    got = _actual_duty_hours(rt, {"2026-08-08": D("11.00")})
    assert got == D("11.00")


def test_duty_override_can_lower_a_day_where_duty_rig_was_winning():
    """The silent no-op this feature exists to fix: with max()-only
    semantics a shorter duty was ignored. Published 4.17, feed duty rig
    5.51 wins; pilot corrects duty down to 10.02h → rig 5.01 → credited."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="9.77")

    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))
    no_override, _, _ = apply_actuals_to_month(baseline, reconciliation)
    lowered, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("10.02")},
    )

    assert no_override.trips[0].effective_pch > lowered.trips[0].effective_pch
    assert lowered.trips[0].effective_pch == D("5.01")


def test_duty_override_never_takes_a_day_below_published():
    """§3.E is structural — max(published, recomputed) still holds.

    NOTE: this passes via _extension_recompute's tolerance short-circuit
    (comp.duty_rig doesn't clear published_pch + duty_tolerance_hours, so
    _apply_duty_extension returns the baseline trip unchanged) — not by
    exercising the max() fold the docstring's headline implies. It does not
    independently prove the max()-floor behavior."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="9.77")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("0.50")},
    )

    assert updated.trips[0].effective_pch == D("4.17")


def test_duty_override_does_not_discard_block_credit():
    """The Aug 8 shape: block 7.13 wins. Shortening duty must NOT cost the
    flight-op credit the pilot never disputed.

    NOTE: this test has no discriminating power against "the override is
    silently ignored" — block (7.13) beats duty-rig regardless of whether
    duty_overrides is threaded at all, so it passes identically either way.
    It does not prove the override was actually applied; see the
    duty-rig-driven tests above for that."""
    baseline_trip = Trip(trip_id="720", published_pch=D("6.08"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _matched_trip("720", actual_block="7.13", packet_pch="6.08",
                       packet_block="6.08", packet_duty="10.73")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    updated, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("8.00")},
    )

    assert updated.trips[0].effective_pch == D("7.13")


def test_duty_override_applies_to_reserve_callout():
    """The silent no-op also hits callouts: this is the ORIGINAL duty-rig
    worked example (report 04:41, ~11h duty, rig 5.51) — a callout day is
    one of the MOST likely places a pilot corrects duty, since duty rig
    genuinely wins there. No override: feed span 12h → padded 13.25h → rig
    6.625 wins over published 4.50. Pilot corrects duty down to 10.02h →
    rig 5.01 → credited instead."""
    callout_date = date(2026, 6, 12)
    rsv = Day(date=callout_date, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="RSV")
    baseline = _empty_month(days=(rsv,))
    rt = _rt_with_span("766", packet_pch="4.50", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="12.0", on_date=callout_date)
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    no_override, _, _ = apply_actuals_to_month(baseline, reconciliation)
    lowered, _, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("10.02")},
    )

    assert no_override.days[0].callout_trip_pch == D("6.625")
    assert lowered.days[0].callout_trip_pch == D("5.01")


def test_duty_override_is_keyed_by_anchorage_local_date_not_utc():
    """Override dict must be keyed by ANC-LOCAL date, not the UTC date — an
    evening ANC departure is already the next day in UTC (Anchorage is
    UTC-8 in summer). This exact class of bug has bitten the codebase three
    times (PRs #42, #52; see timeutil.py header) — a silent no-op on a
    correction is precisely what this feature exists to remove."""
    from nac_pay.timeutil import local_date
    from nac_pay.schedule.apply_actuals import _actual_duty_hours

    start = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)   # 20:00 AKDT Aug 8
    end = start + _hours_to_timedelta(D("2.00"))
    packet = _trip_pairing("720/721", "6.08")
    rt = ReconciledTrip(
        flight_sequence="720/721", legs=(_leg("720", start, end),),
        packet_trip=packet, match_status=MatchStatus.MATCHED,
        first_dt_utc=start, last_dt_utc=end, actual_block_hours=D("2.00"),
    )

    # Sanity: the ANC-local date and the UTC date genuinely differ for this
    # timestamp — the test is meaningless otherwise.
    assert start.date().isoformat() == "2026-08-09"
    assert local_date(start).isoformat() == "2026-08-08"

    # Keyed by ANC-local date ("2026-08-08"), NOT the UTC date ("2026-08-09").
    got = _actual_duty_hours(rt, {"2026-08-08": D("9.00")})
    assert got == D("9.00")


# ── I3: the audit trail must not credit the pilot's own duty to the feed ──


def test_duty_extension_note_flags_pilot_corrected_duty():
    """I3: the DUTY_EXTENSION AssignmentVersion label
    ("Duty extension from iCal: recomputed ...") and the matching
    AppliedEvent detail ("Actual block ... -> recomputed PCH ...") both
    said the recompute came "from iCal"/"from actuals" even when
    credited_duty was substituted outright by a DUTY_CORRECTION
    (_actual_duty_hours). The amount was right; the provenance the pilot
    would show the company in a pay dispute was not. Both must flag the
    override; neither may without one."""
    baseline_trip = Trip(trip_id="766", published_pch=D("4.17"),
                         reason_code=ReasonCode.FLOWN, workdays=1)
    baseline = _empty_month(trips=(baseline_trip,))
    rt = _rt_with_span("766", packet_pch="4.17", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="9.77")
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    no_override, no_events, _ = apply_actuals_to_month(baseline, reconciliation)
    corrected, events, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("10.02")},
    )

    # Sanity: the override genuinely drove this recompute (same fixture as
    # test_duty_override_can_lower_a_day_where_duty_rig_was_winning).
    assert corrected.trips[0].effective_pch == D("5.01")

    corrected_event = next(
        e for e in events if e.kind is AppliedEventKind.DUTY_EXTENSION
    )
    assert "(pilot-corrected duty)" in corrected_event.detail

    corrected_version = corrected.trips[0].versions[-1]
    assert "(pilot-corrected duty)" in corrected_version.label

    # Without the override, the feed-only recompute must not claim a
    # pilot correction that never happened.
    plain_event = next(
        e for e in no_events if e.kind is AppliedEventKind.DUTY_EXTENSION
    )
    assert "(pilot-corrected duty)" not in plain_event.detail
    plain_version = no_override.trips[0].versions[-1]
    assert "(pilot-corrected duty)" not in plain_version.label


def test_reserve_callout_note_flags_pilot_corrected_duty():
    """I3, the third site (apply_actuals.py:249-258): the RESERVE_CALLOUT
    AppliedEvent detail ("Reserve callout to ... (recomputed from actuals,
    published ...)") must flag pilot-corrected provenance the same way the
    other two sites do (test_duty_extension_note_flags_pilot_corrected_duty
    covers :1073/:1090) — this branch gained the ", pilot-corrected duty"
    suffix but was only ever verified by a manual script, with no
    committed test. Same callout fixture as
    test_duty_override_applies_to_reserve_callout (no override: feed span
    12h -> padded rig 6.625 beats published 4.50; corrected duty 10.02h ->
    rig 5.01)."""
    callout_date = date(2026, 6, 12)
    rsv = Day(date=callout_date, duty_type=DutyType.RSV, pch_value=D("3.82"),
              reason_code=ReasonCode.FLOWN, workdays=1, label="RSV")
    baseline = _empty_month(days=(rsv,))
    rt = _rt_with_span("766", packet_pch="4.50", packet_block="4.17",
                       packet_duty="7.0833", actual_block="4.17",
                       span_hours="12.0", on_date=callout_date)
    reconciliation = ReconciliationResult(trips=(rt,), matched=(rt,))

    no_override, no_events, _ = apply_actuals_to_month(baseline, reconciliation)
    corrected, events, _ = apply_actuals_to_month(
        baseline, reconciliation,
        duty_overrides={"2026-06-12": D("10.02")},
    )

    # Sanity: the override genuinely drove this recompute (same fixture as
    # test_duty_override_applies_to_reserve_callout).
    assert corrected.days[0].callout_trip_pch == D("5.01")

    corrected_event = next(
        e for e in events if e.kind is AppliedEventKind.RESERVE_CALLOUT
    )
    assert "pilot-corrected duty" in corrected_event.detail

    # Without the override, the feed-only recompute must not claim a
    # pilot correction that never happened.
    plain_event = next(
        e for e in no_events if e.kind is AppliedEventKind.RESERVE_CALLOUT
    )
    assert "pilot-corrected duty" not in plain_event.detail


# ── Company-assigned PCH folds as a §3.E.1.b candidate (2026-08-11) ──────
#
# Fixture arithmetic (hand-verified, both tests below): reroute signature
# "730/730/731" is unmatched (no packet trip), single leg, actual_block =
# 5.05h, starting 2026-06-12 14:30 UTC.
#   duty_start = first_dt_utc - REPORT_PAD_HOURS(1.00)   = 13:30 UTC
#   duty_end   = last_dt_utc  + TRIP_END_PAD_HOURS(0.25) = 19:48 UTC
#     (last_dt_utc = 14:30 + 5.05h = 19:33 UTC)
#   duty = 19:48 - 13:30 = 6.30h
#   components: flight_op=5.05, duty_rig=6.30/2=3.15,
#               trip_rig=6.30/4.90=1.2857..., cumulative_dpg=1*3.82=3.82
#   trip_pch = max(5.05, 3.15, 1.2857, 3.82) + 0 = 5.05  <- block wins


def test_company_pch_below_the_recompute_credits_the_recompute():
    """The company's reassignment-notice value is one more §3.E.1.b
    candidate, not a replacement: company assigns 4.80, actual times
    recompute to 5.05 -> credited 5.05. (Old behaviour: 4.80 — the
    recompute was silently discarded.) Owner contract 2026-08-11:
    pay MAX(original, company-assigned, recompute)."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on, actual_block="5.05")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {(on.isoformat(), "730/730/731"): "CONFIRMED"}
    overrides = {(on.isoformat(), "730/730/731"): D("4.80")}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions=decisions,
        feed_reassignment_pch_overrides=overrides,
    )

    fr = reassigns[0]
    assert fr.new_pch == D("5.05")
    assert fr.override_pch == D("4.80")
    assert fr.effective_pch == D("5.05")
    assert updated.trips[0].effective_pch == D("5.05")
    # The credited number is the max even though the label may still
    # mention the company PCH — not 4.80.
    assert updated.trips[0].versions[-1].pch_value == D("5.05")


def test_company_pch_above_the_recompute_still_credits_the_company_value():
    """Today's behaviour, still correct: company 5.17 > recompute 5.05
    -> credited 5.17. Pins that the max() did not overshoot."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on, actual_block="5.05")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {(on.isoformat(), "730/730/731"): "CONFIRMED"}
    overrides = {(on.isoformat(), "730/730/731"): D("5.17")}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions=decisions,
        feed_reassignment_pch_overrides=overrides,
    )

    fr = reassigns[0]
    assert fr.new_pch == D("5.05")
    assert fr.override_pch == D("5.17")
    assert fr.effective_pch == D("5.17")
    assert updated.trips[0].effective_pch == D("5.17")
    assert updated.trips[0].versions[-1].pch_value == D("5.17")


def test_no_company_value_is_byte_identical():
    """No override entered -> credited is exactly the recompute, as today."""
    on = date(2026, 6, 12)
    baseline = _empty_month(trips=(_scheduled_trip("730/732", "4.50", on),))
    rt = _unmatched_trip("730/730/731", on_date=on, actual_block="5.05")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))
    decisions = {(on.isoformat(), "730/730/731"): "CONFIRMED"}

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions=decisions,
    )

    fr = reassigns[0]
    assert fr.override_pch is None
    assert fr.new_pch == D("5.05")
    assert fr.effective_pch == D("5.05")
    assert updated.trips[0].effective_pch == D("5.05")


def test_offday_pickup_company_pch_below_recompute_credits_the_recompute():
    """The off-day-pickup site (:377) reads ``pch_overrides`` through the
    identical ``(date_iso, signature)`` key as the reroute site — same
    ``FeedReassignmentDecisionRow`` table, same
    ``/day/{date}/reassignment/confirm`` route, no ``kind`` restriction
    (see ``storage/feed_reassignments.py::pch_overrides_for_month`` and
    ``day.html``'s OFF_DAY_PICKUP branch of the same confirm form). So a
    pickup can carry a company-entered PCH too, and the same §3.E.1.b fold
    applies: company 4.80 vs recompute 5.05 -> credited 5.05."""
    on = date(2026, 6, 12)
    baseline = _empty_month()
    rt = _unmatched_trip("2720/2721", on_date=on, actual_block="5.05")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, _events, reassigns = apply_actuals_to_month(
        baseline, reconciliation,
        feed_reassignment_decisions={(on.isoformat(), "2720/2721"): "CONFIRMED"},
        feed_reassignment_pch_overrides={(on.isoformat(), "2720/2721"): D("4.80")},
    )

    fr = reassigns[0]
    assert fr.new_pch == D("5.05")
    assert fr.override_pch == D("4.80")
    assert fr.effective_pch == D("5.05")
    assert updated.trips[-1].published_pch == D("5.05")
