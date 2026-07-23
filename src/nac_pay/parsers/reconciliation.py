"""Group iCal flight legs into trip instances and match them to packet trips.

Per spec §10:

  ``FLT - <flight#> <ORG>-<DST> <tail>`` — No PCH, no grouping. Reconcile
  against the packet (match key ≈ flight # + date + route + departure
  time) to group legs into trips, inherit published PCH, and rebuild
  trip/duty boundaries. Unmatched legs = open-time pickups /
  reassignments / charter → flag.

Algorithm:

1. Sort legs chronologically by ``dt_start_utc``.
2. Group consecutive legs when the next leg's origin == the previous
   leg's destination AND the layover gap is non-negative and within
   ``layover_max_hours``. A break in either condition starts a new trip.
3. Derive each trip's ``flight_sequence`` from its legs (e.g. legs 768,
   768, 769 → ``"768/768/769"``).
4. Look up the sequence as a trip_id in the packet. Match if found,
   else flag as ``UNMATCHED_NO_PACKET``.

We intentionally key on the **canonical flight sequence** (matching the
packet's ``trip_id``) rather than per-leg flight#-date-time keys. The
sequence is the right semantic identity for a pairing: a callout to a
multi-leg trip preserves the sequence, while a true charter or
mid-month reassignment to a non-bid pairing won't match anything in
the packet — exactly the case the spec wants flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from nac_pay.timeutil import local_date

from .ical_feed import FlightLegEvent, ParsedFeed
from .trip_pairing_packet import TripPairing

# A packet trip can carry a trailing reserve designator: "722/723/R1" means
# fly 722/723, then sit reserve (its TRIP PCH VALUE is the duty-rig over the
# whole 10:45 duty per §3.E.2.a). The iCal feed only shows the *flown*
# portion ("722/723"), so an exact key lookup misses it.
_RESERVE_SUFFIX_RE = re.compile(r"/R\d+$", re.IGNORECASE)


def _match_packet_trip(
    sequence: str, packet: dict[str, TripPairing],
) -> TripPairing | None:
    """Find the packet trip for a flown flight sequence.

    Exact key match wins. Failing that, match a reserve-designator pairing
    by its flying portion — ``"722/723"`` flown matches packet
    ``"722/723/R1"`` (the trailing ``/R<n>`` is reserve, not a leg). Exact
    equality after stripping the suffix keeps this unambiguous (a fully-flown
    ``722/723/750/751`` still matches its own key first)."""
    trip = packet.get(sequence)
    if trip is not None:
        return trip
    for tid, tp in packet.items():
        if _RESERVE_SUFFIX_RE.search(tid) and _RESERVE_SUFFIX_RE.sub("", tid) == sequence:
            return tp
    return None

DEFAULT_LAYOVER_MAX_HOURS: float = 12.0

# A station-chained gap this long that also crosses an Anchorage-local
# midnight is an overnight REST, not a layover: the two sides are different
# civil days' flying. Applied only to UNMATCHED groups — a fused sequence
# that matches a packet pairing is a genuine multi-day trip and stays whole.
# 6.0 splits every legal rest (≥~8h) while never splitting an intra-duty
# midnight turn (gaps well under 6h). See the 2026-07-23 incident: 768/769
# (Jul 24) + 720/721/1780/1781 (Jul 25) fused across an 8.5h overnight gap
# and were attributed to a single day as a bogus 13.21-PCH reassignment.
OVERNIGHT_REST_MIN_HOURS: float = 6.0


class MatchStatus(StrEnum):
    MATCHED = "MATCHED"
    UNMATCHED_NO_PACKET = "UNMATCHED_NO_PACKET"


@dataclass(frozen=True)
class ReconciledTrip:
    """One iCal-derived trip instance, optionally matched to a packet trip.

    ``actual_block_hours`` is the sum of per-leg block from the feed
    (DTEND - DTSTART). When ``packet_trip`` is present, callers wanting
    §3.E.1.b's "greater of published vs recomputed" should compare
    ``published_pch`` against a freshly-recomputed PCH using these actual
    times — that recomputation belongs in the future event-application
    layer, not here.
    """

    flight_sequence: str
    legs: tuple[FlightLegEvent, ...]
    packet_trip: TripPairing | None
    match_status: MatchStatus
    first_dt_utc: datetime
    last_dt_utc: datetime
    actual_block_hours: Decimal

    @property
    def trip_id(self) -> str | None:
        return self.packet_trip.trip_id if self.packet_trip else None

    @property
    def published_pch(self) -> Decimal | None:
        return self.packet_trip.trip_pch_value if self.packet_trip else None

    @property
    def calendar_days_touched(self) -> int:
        """Distinct calendar dates (UTC) covered by the trip's legs.

        Rough workday count — proper §3.D.2 workday counting needs duty-
        period boundaries (one duty period across two calendar days = 1
        workday), which the packet has and this grouping doesn't yet
        reproduce.
        """
        days = {leg.dt_start_utc.date() for leg in self.legs}
        # Also include the final leg's end date in case it spills past midnight UTC.
        if self.legs:
            days.add(self.legs[-1].dt_end_utc.date())
        return len(days)


@dataclass(frozen=True)
class ReconciliationResult:
    trips: tuple[ReconciledTrip, ...]
    matched: tuple[ReconciledTrip, ...] = field(default=())
    unmatched: tuple[ReconciledTrip, ...] = field(default=())


def reconcile_feed_to_packet(
    feed: ParsedFeed,
    packet: dict[str, TripPairing],
    *,
    layover_max_hours: float = DEFAULT_LAYOVER_MAX_HOURS,
) -> ReconciliationResult:
    """Reconcile iCal flight legs against a packet's published trips.

    Returns a ``ReconciliationResult`` with all reconciled trips plus
    pre-partitioned matched / unmatched lists for convenient downstream
    iteration.
    """
    if not feed.flight_legs:
        return ReconciliationResult(trips=())

    grouped = _group_legs_chronologically(feed.flight_legs, layover_max_hours)
    reconciled: list[ReconciledTrip] = []
    for group in grouped:
        rt = _reconcile_one(group, packet)
        if rt.match_status is MatchStatus.MATCHED:
            reconciled.append(rt)
            continue
        # Unmatched: the chronological chain may have fused two civil days'
        # flying across an overnight rest. Split at the rest(s) and re-match
        # each piece — often turning one bogus unmatched group into two
        # cleanly-matched trips (the 2026-07-23 incident).
        parts = _split_at_overnight_rests(group)
        if len(parts) == 1:
            reconciled.append(rt)
        else:
            reconciled.extend(_reconcile_one(part, packet) for part in parts)
    return ReconciliationResult(
        trips=tuple(reconciled),
        matched=tuple(r for r in reconciled if r.match_status is MatchStatus.MATCHED),
        unmatched=tuple(r for r in reconciled if r.match_status is not MatchStatus.MATCHED),
    )


# ── Internals ───────────────────────────────────────────────────────────


def _group_legs_chronologically(
    legs: tuple[FlightLegEvent, ...],
    layover_max_hours: float,
) -> list[list[FlightLegEvent]]:
    sorted_legs = sorted(legs, key=lambda leg: leg.dt_start_utc)
    groups: list[list[FlightLegEvent]] = []
    current: list[FlightLegEvent] = []
    for leg in sorted_legs:
        if not current:
            current = [leg]
            continue
        last = current[-1]
        gap_seconds = (leg.dt_start_utc - last.dt_end_utc).total_seconds()
        gap_hours = gap_seconds / 3600.0
        chains = (
            leg.origin == last.destination
            and 0 <= gap_hours <= layover_max_hours
        )
        if chains:
            current.append(leg)
        else:
            groups.append(current)
            current = [leg]
    if current:
        groups.append(current)
    return groups


def _split_at_overnight_rests(
    group: list[FlightLegEvent],
    min_rest_hours: float = OVERNIGHT_REST_MIN_HOURS,
) -> list[list[FlightLegEvent]]:
    """Split a leg group at overnight rests: gaps that cross an Anchorage-
    local midnight AND are at least ``min_rest_hours`` long. A midnight-
    crossing quick turn (short gap) stays chained; a same-day long ground
    gap stays chained (the FA treats a day's flying as one assignment)."""
    parts: list[list[FlightLegEvent]] = []
    current: list[FlightLegEvent] = [group[0]]
    for leg in group[1:]:
        last = current[-1]
        gap_hours = (leg.dt_start_utc - last.dt_end_utc).total_seconds() / 3600.0
        overnight = (
            gap_hours >= min_rest_hours
            and local_date(leg.dt_start_utc) > local_date(last.dt_end_utc)
        )
        if overnight:
            parts.append(current)
            current = [leg]
        else:
            current.append(leg)
    parts.append(current)
    return parts


def _reconcile_one(
    group: list[FlightLegEvent],
    packet: dict[str, TripPairing],
) -> ReconciledTrip:
    sequence = "/".join(leg.flight_no_short for leg in group)
    packet_trip = _match_packet_trip(sequence, packet)
    status = MatchStatus.MATCHED if packet_trip else MatchStatus.UNMATCHED_NO_PACKET

    actual_block = sum(
        (leg.block_hours for leg in group),
        Decimal("0"),
    )

    return ReconciledTrip(
        flight_sequence=sequence,
        legs=tuple(group),
        packet_trip=packet_trip,
        match_status=status,
        first_dt_utc=group[0].dt_start_utc,
        last_dt_utc=group[-1].dt_end_utc,
        actual_block_hours=actual_block,
    )
