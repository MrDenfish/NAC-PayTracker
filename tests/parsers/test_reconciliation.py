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


# ── Overnight-rest split of unmatched groups (2026-07-23 incident) ─────
def test_unmatched_fused_group_splits_at_overnight_rest():
    """July 23 2026 prod incident: 768/769 flown ANC evening Jul 24 chained
    into 720/721/1780/1781 the next morning (8.5h overnight gap, crosses ANC
    midnight, under the 12h layover cap). The fused 6-leg sequence matches
    nothing; it must split at the rest so each civil day's flying reconciles
    on its own."""
    legs = (
        # 18:00–19:15 / 19:50–21:09 ANC Jul 24 (= 02:00Z+ Jul 25)
        _leg("768", "ANC", "OME", _utc(2026, 7, 25, 2, 0), _utc(2026, 7, 25, 3, 15)),
        _leg("769", "OME", "ANC", _utc(2026, 7, 25, 3, 50), _utc(2026, 7, 25, 5, 9)),
        # 05:41 ANC Jul 25 — 8.53h after 769 in, across ANC-local midnight
        _leg("720", "ANC", "OME", _utc(2026, 7, 25, 13, 41), _utc(2026, 7, 25, 15, 11)),
        _leg("721", "OME", "ANC", _utc(2026, 7, 25, 16, 1), _utc(2026, 7, 25, 17, 26)),
        _leg("1780", "ANC", "DGG", _utc(2026, 7, 25, 19, 0), _utc(2026, 7, 25, 20, 35)),
        _leg("1781", "DGG", "ANC", _utc(2026, 7, 25, 21, 35), _utc(2026, 7, 25, 23, 10)),
    )
    packet = {"768/769": object(), "720/721/1780/1781": object()}
    result = reconcile_feed_to_packet(ParsedFeed(flight_legs=legs), packet)

    assert [t.flight_sequence for t in result.trips] == [
        "768/769", "720/721/1780/1781",
    ]
    assert all(t.match_status is MatchStatus.MATCHED for t in result.trips)


def test_unmatched_redeye_with_short_turn_stays_fused():
    """A midnight-crossing quick turn (55 min) is NOT a rest — the group
    must stay whole even though it's unmatched."""
    legs = (
        # 22:30–23:45 ANC, then 00:40–01:55 ANC the next civil day
        _leg("990", "ANC", "OME", _utc(2026, 7, 25, 6, 30), _utc(2026, 7, 25, 7, 45)),
        _leg("991", "OME", "ANC", _utc(2026, 7, 25, 8, 40), _utc(2026, 7, 25, 9, 55)),
    )
    result = reconcile_feed_to_packet(ParsedFeed(flight_legs=legs), {})
    assert len(result.trips) == 1
    assert result.trips[0].flight_sequence == "990/991"


def test_matched_multiday_group_is_never_split():
    """If the fused sequence matches a packet pairing (a genuine multi-day
    trip), the overnight split must not run."""
    legs = (
        _leg("900", "ANC", "OME", _utc(2026, 7, 25, 2, 0), _utc(2026, 7, 25, 3, 15)),
        # 8.5h overnight rest in OME, crosses ANC midnight
        _leg("901", "OME", "ANC", _utc(2026, 7, 25, 11, 45), _utc(2026, 7, 25, 13, 0)),
    )
    packet = {"900/901": object()}
    result = reconcile_feed_to_packet(ParsedFeed(flight_legs=legs), packet)
    assert len(result.trips) == 1
    assert result.trips[0].match_status is MatchStatus.MATCHED


def test_calendar_days_touched_uses_anchorage_local_dates():
    """A 15:00→17:00 ANC afternoon leg spans 23:00Z→01:00Z — two UTC dates,
    ONE civil day. Days-touched feeds workday counting (cumulative DPG in
    the off-day pickup recompute) and must be local, not UTC."""
    legs = (
        _leg("768", "ANC", "OME", _utc(2026, 7, 24, 23, 0), _utc(2026, 7, 25, 1, 0)),
    )
    result = reconcile_feed_to_packet(ParsedFeed(flight_legs=legs), {})
    assert result.trips[0].calendar_days_touched == 1
