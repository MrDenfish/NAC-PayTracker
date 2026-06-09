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

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .ical_feed import FlightLegEvent, ParsedFeed
from .trip_pairing_packet import TripPairing

DEFAULT_LAYOVER_MAX_HOURS: float = 12.0


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
    reconciled: list[ReconciledTrip] = [_reconcile_one(group, packet) for group in grouped]
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


def _reconcile_one(
    group: list[FlightLegEvent],
    packet: dict[str, TripPairing],
) -> ReconciledTrip:
    sequence = "/".join(leg.flight_no_short for leg in group)
    packet_trip = packet.get(sequence)
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
