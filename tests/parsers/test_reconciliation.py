"""iCal-to-packet reconciliation tests.

The sample iCal at docs/iCal_schedule_feed.ics covers Dennis FISHER's
June 7-29 2026 roster — 7 flight legs across two distinct trips. Both
trips' flight sequences are present in the June 2026 packet, so a full
reconciliation should match both.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.parsers import (
    FlightLegEvent,
    MatchStatus,
    ParsedFeed,
    parse_ical_feed,
    parse_trip_pairing_packet,
    reconcile_feed_to_packet,
)

D = Decimal
DOCS = Path(__file__).resolve().parents[2] / "docs"
ICAL = DOCS / "iCal_schedule_feed.ics"
PACKET = DOCS / "JUNE 2026 Trip Pairing Packet.pdf"


# ── Reserve-designator packet pairings (722/723 ↔ 722/723/R1) ──────────


def test_match_packet_trip_resolves_reserve_designator_pairing():
    """The feed shows only the flown portion ("722/723"); the packet keys
    the pairing with its reserve tail ("722/723/R1"). They must reconcile —
    otherwise the flown trip is wrongly flagged unmatched (the July 722/R1
    false positive)."""
    from nac_pay.parsers.reconciliation import _match_packet_trip

    reserve_trip = object()
    full_trip = object()
    packet = {"722/723/R1": reserve_trip, "722/723/750/751": full_trip}

    assert _match_packet_trip("722/723", packet) is reserve_trip
    # A fully-flown pairing still matches its own exact key first.
    assert _match_packet_trip("722/723/750/751", packet) is full_trip
    # No spurious match.
    assert _match_packet_trip("999/998", packet) is None


# ── End-to-end: sample feed + June packet ──────────────────────────────
@pytest.fixture(scope="module")
def reconciled():
    feed = parse_ical_feed(ICAL)
    packet = parse_trip_pairing_packet(str(PACKET))
    return reconcile_feed_to_packet(feed, packet)


def test_two_trips_grouped_from_seven_legs(reconciled):
    """7 chained iCal legs collapse to 2 distinct trip instances."""
    assert len(reconciled.trips) == 2


def test_both_trips_matched_to_packet(reconciled):
    assert len(reconciled.matched) == 2
    assert reconciled.unmatched == ()
    assert all(t.match_status is MatchStatus.MATCHED for t in reconciled.trips)


def test_first_trip_is_768_768_769_on_june_12(reconciled):
    trip = reconciled.trips[0]
    assert trip.flight_sequence == "768/768/769"
    assert trip.trip_id == "768/768/769"
    assert trip.published_pch == D("4.17")
    assert len(trip.legs) == 3
    assert trip.first_dt_utc.date().isoformat() == "2026-06-12"
    assert trip.calendar_days_touched == 1


def test_second_trip_is_722_723_754_755_on_june_17(reconciled):
    trip = reconciled.trips[1]
    assert trip.flight_sequence == "722/723/754/755"
    assert trip.published_pch == D("5.25")
    assert len(trip.legs) == 4
    assert trip.first_dt_utc.date().isoformat() == "2026-06-17"


def test_actual_block_matches_packet_block_when_no_extension(reconciled):
    """When the feed times match the packet times, the §3.E.1.b max-of
    comparison is a no-op — neither side wins, both equal."""
    for trip in reconciled.trips:
        assert trip.actual_block_hours == trip.packet_trip.sch_block_hours


# ── Grouping logic isolated ────────────────────────────────────────────
def _leg(
    flight_short: str,
    org: str,
    dst: str,
    start: datetime,
    end: datetime,
    *,
    tail: str = "N000XX",
    fo: str = "Dennis FISHER",
) -> FlightLegEvent:
    return FlightLegEvent(
        uid=f"test-{flight_short}-{start.isoformat()}",
        dt_start_utc=start,
        dt_end_utc=end,
        flight_no_raw=f"NC{flight_short}",
        flight_no_short=flight_short,
        origin=org,
        destination=dst,
        tail=tail,
        customer="Test",
        captain="",
        first_officer=fo,
    )


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_long_layover_splits_a_group():
    """A 13-hour gap (> default 12h layover threshold) splits one trip
    into two distinct trip instances."""
    leg_a = _leg("100", "ANC", "BRW", _utc(2026, 6, 1, 14), _utc(2026, 6, 1, 16))
    # 13-hour gap to next leg → should NOT chain
    leg_b = _leg("101", "BRW", "ANC", _utc(2026, 6, 2, 5), _utc(2026, 6, 2, 7))
    feed = ParsedFeed(flight_legs=(leg_a, leg_b))
    result = reconcile_feed_to_packet(feed, {})
    assert len(result.trips) == 2


def test_broken_route_chain_splits_a_group():
    """Even with a short gap, a leg whose origin doesn't match the
    previous destination starts a new trip."""
    leg_a = _leg("100", "ANC", "BRW", _utc(2026, 6, 1, 14), _utc(2026, 6, 1, 16))
    leg_b = _leg("101", "SCC", "ANC", _utc(2026, 6, 1, 17), _utc(2026, 6, 1, 19))
    # BRW (a's dst) ≠ SCC (b's org) → split
    feed = ParsedFeed(flight_legs=(leg_a, leg_b))
    result = reconcile_feed_to_packet(feed, {})
    assert len(result.trips) == 2


def test_unsorted_input_still_groups_correctly():
    """Legs supplied out of order are sorted chronologically before grouping."""
    leg1 = _leg("A", "ANC", "BRW", _utc(2026, 6, 1, 14), _utc(2026, 6, 1, 16))
    leg2 = _leg("B", "BRW", "SCC", _utc(2026, 6, 1, 17), _utc(2026, 6, 1, 18))
    leg3 = _leg("C", "SCC", "ANC", _utc(2026, 6, 1, 19), _utc(2026, 6, 1, 21))
    feed = ParsedFeed(flight_legs=(leg3, leg1, leg2))   # scrambled
    result = reconcile_feed_to_packet(feed, {})
    assert len(result.trips) == 1
    assert result.trips[0].flight_sequence == "A/B/C"


# ── Unmatched / unknown trips ──────────────────────────────────────────
def test_unknown_sequence_flagged_as_unmatched():
    """A trip whose flight sequence doesn't appear in the packet (charter,
    new reassignment, etc.) lands in `unmatched` with no published_pch."""
    leg = _leg("9999", "ANC", "BRW", _utc(2026, 6, 1, 14), _utc(2026, 6, 1, 16))
    feed = ParsedFeed(flight_legs=(leg,))
    packet_with_other_trips = {"766/766/767": None}   # type-checking only; not lookup
    # Build a real packet dict with a SINGLE unrelated trip key
    real_packet = parse_trip_pairing_packet(str(PACKET))
    result = reconcile_feed_to_packet(feed, real_packet)
    assert len(result.unmatched) == 1
    flagged = result.unmatched[0]
    assert flagged.match_status is MatchStatus.UNMATCHED_NO_PACKET
    assert flagged.published_pch is None
    assert flagged.trip_id is None


# ── Empty / degenerate inputs ──────────────────────────────────────────
def test_empty_feed_returns_no_trips():
    feed = ParsedFeed(flight_legs=())
    result = reconcile_feed_to_packet(feed, {})
    assert result.trips == ()
    assert result.matched == ()
    assert result.unmatched == ()


def test_single_leg_trip_handled():
    leg = _leg("R1", "ANC", "ANC", _utc(2026, 6, 1, 14), _utc(2026, 6, 1, 19, 45))
    feed = ParsedFeed(flight_legs=(leg,))
    result = reconcile_feed_to_packet(feed, {})
    assert len(result.trips) == 1
    assert result.trips[0].flight_sequence == "R1"


# ── §3.E.1.b setup: duty extension via iCal vs packet ──────────────────
def test_actual_block_differs_when_iCal_leg_extended(reconciled):
    """Synthetic: extend the first leg's DTEND by 30 minutes — the
    reconciled trip's actual_block_hours rises but packet block stays.
    This is the data the future event-application layer will use to fire
    a §3.E.1.b reassignment greater-of test."""
    trip = reconciled.trips[0]
    extended_leg = replace(
        trip.legs[0],
        dt_end_utc=trip.legs[0].dt_end_utc + timedelta(minutes=30),
    )
    extended_feed = ParsedFeed(
        flight_legs=(extended_leg, *trip.legs[1:]),
    )
    packet = parse_trip_pairing_packet(str(PACKET))
    result = reconcile_feed_to_packet(extended_feed, packet)
    rt = result.trips[0]
    assert rt.actual_block_hours > rt.packet_trip.sch_block_hours
    # 30 minutes = 0.5h delta exactly (no rounding through Decimal/float)
    assert rt.actual_block_hours - rt.packet_trip.sch_block_hours == D("0.5")
