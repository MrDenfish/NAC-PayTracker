"""Trip Pairing Packet parser + §9 validation tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from nac_pay.parsers import (
    parse_trip_pairing_packet,
    validate_trip_pairing_packet,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
MAY_PACKET = DOCS / "MAY  2026  Trip Pairing Packet.pdf"
JUN_PACKET = DOCS / "JUNE 2026 Trip Pairing Packet.pdf"

D = Decimal


# ── May packet ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def may_packet():
    return parse_trip_pairing_packet(str(MAY_PACKET))


def test_may_packet_parses_10_standard_trips(may_packet):
    assert len(may_packet) == 10


def test_may_packet_includes_flt_766(may_packet):
    """FLT 766 is the trip on FISHER's May 1 line — must be present."""
    assert "766/766/767" in may_packet


def test_may_flt_766_components_match_hand_check(may_packet):
    """Spot check against the hand-verified values from
    tests/engine/test_reassignment.py::TestFlt766FromMayPacket."""
    trip = may_packet["766/766/767"]
    assert trip.trip_pch_value == D("4.17")
    assert trip.flight_op_pch == D("4.17")
    assert trip.duty_rig_pch == D("3.54")
    assert trip.trip_rig_pch == D("1.45")
    assert trip.cumulative_dpg_pch == D("3.82")
    assert trip.deadhead_pch == D("0.00")
    assert trip.workdays == 1
    assert trip.start_day_of_week == "Sunday"
    assert trip.end_day_of_week == "Sunday"


def test_may_flt_766_raw_times_extract_correctly(may_packet):
    """HH:MM strings convert to Decimal hours: 4:10 → 4 + 10/60 ≈ 4.167."""
    trip = may_packet["766/766/767"]
    # Block 4:10
    assert trip.sch_block_hours == D("4") + D("10") / D("60")
    # Duty 7:05
    assert trip.duty_hours == D("7") + D("5") / D("60")
    # TAFB 7:05
    assert trip.tafb_hours == trip.duty_hours
    assert trip.total_dh_hours == D("0")


def test_may_flt_766_scheduled_duty_window(may_packet):
    """L Day Show / L Day Duty Off parse to local HH:MM, and their span equals
    the printed duty — the reconstruct-from-packet fallback source."""
    trip = may_packet["766/766/767"]
    assert trip.sched_duty_on and trip.sched_duty_off       # both "HH:MM"
    assert len(trip.sched_duty_on) == 5 and trip.sched_duty_on[2] == ":"

    def _mins(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    span_min = (_mins(trip.sched_duty_off) - _mins(trip.sched_duty_on)) % (24 * 60)
    assert D(span_min) == trip.duty_hours * D("60")


def test_may_raw_trip_id_keeps_trailing_slashes(may_packet):
    """raw_trip_id preserves the packet form, trip_id strips them."""
    trip = may_packet["766/766/767"]
    assert trip.raw_trip_id.startswith("766/766/767")
    assert trip.raw_trip_id.endswith("/")     # trailing filler
    assert trip.trip_id == "766/766/767"      # canonical, no trailing /


def test_may_skips_non_trip_pages(may_packet):
    """The packet has 16 pages but only 10 are standard trips; the rest
    (cover, weekly summary, R1 reserve standby) are silently skipped."""
    assert len(may_packet) == 10
    page_indexes = {t.page_index for t in may_packet.values()}
    # Trips live on pages 4..15 (1-indexed); we don't pin exact indexes,
    # just that they don't include page 1 (cover) or page 16 (R1 standby).
    assert 0 not in page_indexes
    assert 15 not in page_indexes


# ── §9 monthly validation ───────────────────────────────────────────────
def test_may_packet_validates_clean(may_packet):
    """A clean packet from operations should produce zero discrepancies —
    every printed component should be reproducible from the raw times."""
    discrepancies = validate_trip_pairing_packet(may_packet)
    assert discrepancies == []


def test_validator_detects_injected_component_mismatch(may_packet):
    """Mutate FLT 766's printed Flight Op PCH to 9.99 — validator must
    flag the trip with a non-trivial delta."""
    trip = may_packet["766/766/767"]
    mutated = {**may_packet, trip.trip_id: replace(trip, flight_op_pch=D("9.99"))}
    discrepancies = validate_trip_pairing_packet(mutated)
    fields = {d.field for d in discrepancies if d.trip_id == trip.trip_id}
    assert "flight_op_pch" in fields
    # The trip value max would also shift since recomputed_flight_op stays at 4.17
    # but printed trip_pch_value still says 4.17 → no trip_pch_value discrepancy
    # in this specific mutation. We only check the injected field shows up.


def test_validator_detects_injected_trip_pch_value_mismatch(may_packet):
    trip = may_packet["766/766/767"]
    mutated = {**may_packet, trip.trip_id: replace(trip, trip_pch_value=D("9.99"))}
    discrepancies = validate_trip_pairing_packet(mutated)
    fields = {d.field for d in discrepancies if d.trip_id == trip.trip_id}
    assert "trip_pch_value" in fields


def test_validator_tolerance_absorbs_subcent_rounding(may_packet):
    """A 0.005 delta is within tolerance (±0.01) — not flagged."""
    trip = may_packet["766/766/767"]
    # Bump printed value by less than the tolerance
    mutated = {**may_packet, trip.trip_id: replace(trip, flight_op_pch=D("4.175"))}
    discrepancies = validate_trip_pairing_packet(mutated)
    fields = {d.field for d in discrepancies if d.trip_id == trip.trip_id}
    assert "flight_op_pch" not in fields


# ── June packet (smoke) ────────────────────────────────────────────────
def test_june_packet_parses_and_validates_clean():
    packet = parse_trip_pairing_packet(str(JUN_PACKET))
    assert len(packet) >= 5     # June has trips of the standard format
    assert validate_trip_pairing_packet(packet) == []
