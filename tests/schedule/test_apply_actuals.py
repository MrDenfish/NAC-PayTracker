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
) -> ReconciledTrip:
    start = datetime(on_date.year, on_date.month, on_date.day, 14, 30, tzinfo=timezone.utc)
    end = start + _hours_to_timedelta(D("2.5"))
    leg = _leg(flight_sequence, start, end)
    return ReconciledTrip(
        flight_sequence=flight_sequence,
        legs=(leg,),
        packet_trip=None,
        match_status=MatchStatus.UNMATCHED_NO_PACKET,
        first_dt_utc=start,
        last_dt_utc=end,
        actual_block_hours=D("2.5"),
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

    updated, events = apply_actuals_to_month(baseline, reconciliation)

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

    updated, events = apply_actuals_to_month(baseline, reconciliation)

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

    updated, events = apply_actuals_to_month(baseline, reconciliation)
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

    updated, events = apply_actuals_to_month(baseline, reconciliation)

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

    updated, _ = apply_actuals_to_month(baseline, reconciliation)
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

    updated, events = apply_actuals_to_month(baseline, reconciliation)

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

    updated, events = apply_actuals_to_month(baseline, reconciliation)

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
    updated, _ = apply_actuals_to_month(baseline, reconciliation)

    # First trip gets the version; second is untouched.
    assert len(updated.trips[0].versions) == 1
    assert updated.trips[1].versions == ()


# ── Unmatched ──────────────────────────────────────────────────────────


def test_unmatched_trip_logged_but_not_added():
    """An unmatched reconciled trip (no packet match) must NOT be silently
    added — the Month is unchanged, only an event is logged for review."""
    baseline = _empty_month()
    rt = _unmatched_trip(flight_sequence="9999/9998")
    reconciliation = ReconciliationResult(trips=(rt,), unmatched=(rt,))

    updated, events = apply_actuals_to_month(baseline, reconciliation)

    assert updated.trips == ()
    assert updated.days == ()
    review = [e for e in events if e.kind is AppliedEventKind.UNMATCHED_TRIP_REVIEW]
    assert len(review) == 1
    assert review[0].trip_id is None
    assert review[0].delta_pch is None
    assert "9999/9998" in review[0].detail


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
    updated, applied = apply_actuals_to_month(baseline, reconciliation)

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
