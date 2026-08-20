"""iCal feed parser tests against the sample at docs/iCal_schedule_feed.ics."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.parsers import (
    DeadheadLegEvent,
    FlightLegEvent,
    OffEvent,
    ReserveEvent,
    UnknownEvent,
    parse_ical_feed,
)

D = Decimal
DOCS = Path(__file__).resolve().parents[2] / "docs"
SAMPLE = DOCS / "iCal_schedule_feed.ics"
SAMPLE_AUG = DOCS / "iCal_schedule_feed_aug_2026.ics"


@pytest.fixture(scope="module")
def feed():
    return parse_ical_feed(SAMPLE)


@pytest.fixture(scope="module")
def feed_aug():
    """Live pull captured 2026-08-19 — first specimen with POG deadhead,
    STP stopover, and GCO training events (Dennis FISHER, Aug 18-Sep 1)."""
    return parse_ical_feed(SAMPLE_AUG)


# ── Event-count sanity ──────────────────────────────────────────────────
def test_feed_partitions_events_by_type(feed):
    """Sample covers June 7-29 2026 for Dennis FISHER: 7 FLT legs across two
    trips (June 12 = 3 legs, June 17 = 4 legs), 9 R/S reserve days, 12 LEA OFF
    days, no unknown formats yet."""
    assert len(feed.flight_legs) == 7
    assert len(feed.reserves) == 9
    assert len(feed.off_days) == 12
    assert feed.unknown == ()
    assert feed.total_events == 28


# ── FLT events ─────────────────────────────────────────────────────────
def test_first_flt_leg_decoded_fully(feed):
    leg = feed.flight_legs[0]
    assert isinstance(leg, FlightLegEvent)
    assert leg.flight_no_raw == "NC768"
    assert leg.flight_no_short == "768"     # NC prefix stripped for matching
    assert leg.origin == "ANC"
    assert leg.destination == "BRW"
    assert leg.tail == "N409YK"
    assert leg.dt_start_utc == datetime(2026, 6, 12, 14, 30, tzinfo=timezone.utc)
    assert leg.dt_end_utc == datetime(2026, 6, 12, 16, 20, tzinfo=timezone.utc)
    assert leg.customer == "Northern Air Cargo"
    assert leg.captain == "Timo Armas SAARINEN"
    assert leg.first_officer == "Dennis FISHER"


def test_block_hours_derives_from_dtstart_dtend(feed):
    """First leg: 14:30 → 16:20 UTC = 1h50m = 1.8333… hours."""
    leg = feed.flight_legs[0]
    expected = D("1") + D("50") / D("60")
    assert abs(leg.block_hours - expected) < D("0.0001")


def test_pilot_name_consistent_across_all_legs(feed):
    """Every FLT in this sample has Dennis FISHER as FO — the iCal sample
    is genuinely his roster (confirms DFI in tests/integration is the right
    pilot to target with this feed)."""
    assert all(leg.first_officer == "Dennis FISHER" for leg in feed.flight_legs)


def test_two_trips_distinguishable_by_date(feed):
    """The 7 legs split into two trips by date: 3 on June 12, 4 on June 17."""
    dates = [leg.dt_start_utc.date() for leg in feed.flight_legs]
    counts = {d: dates.count(d) for d in set(dates)}
    from datetime import date

    assert counts[date(2026, 6, 12)] == 3
    assert counts[date(2026, 6, 17)] == 4


# ── R/S reserve events ────────────────────────────────────────────────
def test_reserve_event_parsing(feed):
    rs = feed.reserves[0]
    assert isinstance(rs, ReserveEvent)
    assert rs.base == "ANC"
    assert rs.line_designator == "1021S"
    assert rs.line_designator_short == "1021"   # strips trailing 'S' for MS match
    assert rs.dt_start_utc == datetime(2026, 6, 7, 11, 0, tzinfo=timezone.utc)
    assert rs.dt_end_utc == datetime(2026, 6, 7, 23, 0, tzinfo=timezone.utc)


def test_all_reserves_in_sample_match_1021_line(feed):
    """Sample is from a single pilot's roster — all R/S events should be
    on the same reserve line."""
    assert all(rs.line_designator_short == "1021" for rs in feed.reserves)


# ── LEA OFF events ─────────────────────────────────────────────────────
def test_off_event_is_24_hour_block(feed):
    off = feed.off_days[0]
    assert isinstance(off, OffEvent)
    assert off.label == "OFF"
    duration = off.dt_end_utc - off.dt_start_utc
    # 08:00Z → next day 07:59Z = 23h59m, just shy of 24h (intentional avoidance
    # of midnight crossing). Tolerance ±1 minute.
    assert abs(duration.total_seconds() - 86400) < 120


# ── POG deadhead events (August 2026 live specimen) ───────────────────
def test_aug_feed_partitions_events_by_type(feed_aug):
    """Aug 18-Sep 1 pull: 20 FLT legs, 2 POG deadheads (MIA→DFW→ANC return
    from recurrent training), 3 R/S days, 4 LEA days (3 OFF + one 24/7),
    and 3 not-yet-typed events (2 STP stopovers, 1 GCO training)."""
    assert len(feed_aug.flight_legs) == 20
    assert len(feed_aug.deadhead_legs) == 2
    assert len(feed_aug.reserves) == 3
    assert len(feed_aug.off_days) == 4
    assert len(feed_aug.unknown) == 3
    assert feed_aug.total_events == 32


def test_deadhead_leg_decoded_fully(feed_aug):
    dh = feed_aug.deadhead_legs[0]
    assert isinstance(dh, DeadheadLegEvent)
    assert dh.origin == "MIA"
    assert dh.destination == "DFW"
    assert dh.carrier == "American Airlines"
    assert dh.pilot == "Dennis FISHER"       # "(p)" passenger marker stripped
    assert dh.dt_start_utc == datetime(2026, 8, 20, 18, 5, tzinfo=timezone.utc)
    assert dh.dt_end_utc == datetime(2026, 8, 20, 21, 29, tzinfo=timezone.utc)


def test_deadhead_block_hours(feed_aug):
    """MIA→DFW 18:05→21:29 = 3h24m = 3.4h; DFW→ANC 23:35→06:32 = 6h57m = 6.95h."""
    first, second = feed_aug.deadhead_legs
    assert first.block_hours == D("3.4")
    assert second.block_hours == D("6.95")
    assert second.destination == "ANC"


def test_stopover_and_training_stay_unknown(feed_aug):
    """STP/GCO have no pay rule yet — they must land in unknown (fail open),
    not be silently misfiled as flights or deadheads."""
    summaries = {u.summary for u in feed_aug.unknown}
    assert summaries == {
        "STP - Stop over at MIA",
        "GCO - Recurrent Training - Northern Air Cargo",
    }


def test_lea_247_rides_the_off_prefix(feed_aug):
    """LEA - 24/7 (the contractual 24-in-7 day off) parses as an OffEvent
    with its label preserved, distinguishable from a plain OFF."""
    labels = sorted(o.label for o in feed_aug.off_days)
    assert labels == ["24/7", "OFF", "OFF", "OFF"]


# ── Unknown / forward-compat ──────────────────────────────────────────
def test_unknown_prefixes_are_isolated_not_dropped():
    """A synthetic feed with a CLASS event (spec §10's deferred type) lands
    in unknown so the GUI can flag it for review — proves the parser fails
    open, not silently."""
    synthetic = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:test-1
DTSTART:20260601T140000Z
DTEND:20260601T180000Z
SUMMARY:CLASS - Recurrent Ground School
DESCRIPTION:Training day
END:VEVENT
END:VCALENDAR
"""
    feed = parse_ical_feed(synthetic)
    assert len(feed.unknown) == 1
    u = feed.unknown[0]
    assert isinstance(u, UnknownEvent)
    assert u.summary == "CLASS - Recurrent Ground School"
    assert u.description == "Training day"
    assert feed.flight_legs == ()
    assert feed.reserves == ()
    assert feed.off_days == ()


def test_parse_accepts_path_string_and_bytes():
    """Caller convenience: same result from a Path, a path string, or raw bytes."""
    from_path = parse_ical_feed(SAMPLE)
    from_str = parse_ical_feed(str(SAMPLE))
    from_bytes = parse_ical_feed(SAMPLE.read_bytes())
    assert from_path.total_events == from_str.total_events == from_bytes.total_events
