"""Apply mid-month actuals (iCal + packet reconciliation) onto a baseline Month.

Input:
- ``baseline``: a Month constructed from the Final Award (what was scheduled).
- ``reconciliation``: ReconciliationResult from ``parsers.reconcile_feed_to_packet``
  — iCal flight legs grouped into trip instances and matched to packet trips.

Output:
- An updated ``Month`` with events applied (duty extensions, reserve callouts,
  open-time pickups).
- A log of ``AppliedEvent`` records explaining what was applied — surfaces in
  the GUI's discrepancies / activity view.

What's detected here (auto-applied):

- **Duty extension (§3.E.1.b)**: matched trip whose iCal actual block exceeds
  the packet's printed block by more than a tolerance. We add an
  ``AssignmentVersion`` with the recomputed §3.E components from the actual
  block + duty (duty ≈ first-leg start → last-leg end span). The engine's
  ``Trip.effective_pch`` then pays ``max(published, recomputed)``.
- **Reserve callout (§3.F)**: matched trip with no baseline Trip of that id but
  the first leg's date hits a baseline RSV Day. We set ``Day.callout_trip_pch``;
  lowering emits a TRIP chunk at ``max(DPG, callout_pch)`` + an
  INVOLUNTARY_EXCESS floor event for any excess.
- **Open-time pickup**: matched trip with no baseline Trip *and* no baseline
  RSV day on its dates. We add a new Trip with reason FLOWN and premium
  ``OPEN_TIME_BID_PERIOD`` (1.0× — the safer default; pilot can promote to
  the 1.5× mid-month premium in the GUI when it qualifies).

Trip-id matching: the Final Award prints a short-form aid per day cell
("768") while the packet uses the full sequence ("768/768/769"). We match
by ordered-subsequence (aid segments must appear in order within packet
segments). Baseline trips are tracked by **index**, not aid, so duplicate
aids on different dates (e.g. FISHER's ``"722/754"`` on June 6 *and* June
17) don't collide.

What's NOT auto-applied (pilot-driven via GUI in the future):

- Voluntary drops / lesser trades / unprotected unavailability — not
  detectable from iCal alone (the feed shows absence but not the *why*).
- Sick / protected absences / charter — same.

Unmatched reconciled trips (no packet entry) are logged but NOT added to the
Month automatically. They need pilot categorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date as date_t
from decimal import Decimal
from enum import StrEnum

from nac_pay.engine.constants import DPG
from nac_pay.engine.trip_pch import (
    components_from_times,
    effective_trip_pch_after_reassignment,
)
from nac_pay.parsers import ReconciledTrip, ReconciliationResult

from .labels import EntryMode, PremiumCategory, ReasonCode
from .models import AssignmentVersion, Day, Month, Trip

_DUTY_EXTENSION_TOLERANCE_HOURS: Decimal = Decimal("0.05")  # 3 minutes


class AppliedEventKind(StrEnum):
    DUTY_EXTENSION = "DUTY_EXTENSION"
    RESERVE_CALLOUT = "RESERVE_CALLOUT"
    OPEN_TIME_PICKUP = "OPEN_TIME_PICKUP"
    UNMATCHED_TRIP_REVIEW = "UNMATCHED_TRIP_REVIEW"


@dataclass(frozen=True)
class AppliedEvent:
    kind: AppliedEventKind
    date: date_t
    trip_id: str | None
    detail: str
    delta_pch: Decimal | None

    def __str__(self) -> str:
        sign = f" Δ{self.delta_pch:+.2f}" if self.delta_pch is not None else ""
        return f"{self.date} {self.kind.value}: {self.detail}{sign}"


def apply_actuals_to_month(
    baseline: Month,
    reconciliation: ReconciliationResult,
    *,
    duty_extension_tolerance_hours: Decimal = _DUTY_EXTENSION_TOLERANCE_HOURS,
) -> tuple[Month, tuple[AppliedEvent, ...]]:
    """Apply mid-month events from a reconciliation onto a baseline Month.

    Returns the updated Month and a tuple of AppliedEvent records.
    """
    # Index baseline trips by aid → list of indexes (handles duplicate aids).
    aid_to_indexes: dict[str, list[int]] = {}
    for idx, trip in enumerate(baseline.trips):
        aid_to_indexes.setdefault(trip.trip_id, []).append(idx)
    aid_segments: list[tuple[str, tuple[str, ...]]] = [
        (aid, _flying_segments(aid)) for aid in aid_to_indexes
    ]

    baseline_rsv_by_date: dict[date_t, Day] = {
        d.date: d
        for d in baseline.days
        if d.date is not None and _is_reserve_day(d)
    }

    matched_indexes: set[int] = set()
    duty_extension_by_index: dict[int, Trip] = {}
    pickups: list[Trip] = []
    callout_pch_by_date: dict[date_t, Decimal] = {}
    callout_aid_by_date: dict[date_t, str] = {}
    events: list[AppliedEvent] = []

    # ── Matched reconciled trips ─────────────────────────────────────────
    for rt in reconciliation.matched:
        first_date = rt.first_dt_utc.date()
        matched_aid = _find_baseline_aid_for_packet_trip(rt.trip_id, aid_segments)
        idx = _next_unmatched_index_for_aid(
            matched_aid, aid_to_indexes, matched_indexes,
            baseline.trips, first_date,
        )

        if idx is not None:
            matched_indexes.add(idx)
            baseline_trip = baseline.trips[idx]
            extended = _apply_duty_extension(
                baseline_trip, rt, duty_extension_tolerance_hours, events,
            )
            if extended is not baseline_trip:
                duty_extension_by_index[idx] = extended
        elif (
            first_date in baseline_rsv_by_date
            and first_date not in callout_pch_by_date
        ):
            callout_pch_by_date[first_date] = rt.published_pch
            callout_aid_by_date[first_date] = rt.trip_id
            excess = max(Decimal("0"), rt.published_pch - DPG)
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.RESERVE_CALLOUT,
                    date=first_date,
                    trip_id=rt.trip_id,
                    detail=f"Reserve callout to {rt.trip_id} (pch={rt.published_pch})",
                    delta_pch=excess,
                )
            )
        else:
            pickups.append(
                Trip(
                    trip_id=rt.trip_id,
                    published_pch=rt.published_pch,
                    reason_code=ReasonCode.FLOWN,
                    premium_category=PremiumCategory.OPEN_TIME_BID_PERIOD,
                    workdays=rt.calendar_days_touched,
                    entry_mode=EntryMode.SIMPLE,
                    label=f"Mid-month pickup {rt.trip_id} on {first_date.isoformat()}",
                    dates=(first_date,),
                )
            )
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.OPEN_TIME_PICKUP,
                    date=first_date,
                    trip_id=rt.trip_id,
                    detail=(
                        f"Pickup or reassignment to {rt.trip_id} "
                        f"(pch={rt.published_pch}); defaulted to 1.0× — "
                        "promote to OPEN_TIME_MID_MONTH if it qualified for premium"
                    ),
                    delta_pch=rt.published_pch,
                )
            )

    # ── Unmatched reconciled trips: log only ─────────────────────────────
    for rt in reconciliation.unmatched:
        events.append(
            AppliedEvent(
                kind=AppliedEventKind.UNMATCHED_TRIP_REVIEW,
                date=rt.first_dt_utc.date(),
                trip_id=None,
                detail=(
                    f"Flew sequence {rt.flight_sequence} ({len(rt.legs)} legs, "
                    f"actual_block={rt.actual_block_hours:.2f}h) — not in packet; "
                    "needs pilot categorization (charter? non-bid reassignment?)"
                ),
                delta_pch=None,
            )
        )

    # ── Rebuild trip list preserving baseline order ──────────────────────
    final_trips: list[Trip] = []
    for idx, trip in enumerate(baseline.trips):
        if idx in duty_extension_by_index:
            final_trips.append(duty_extension_by_index[idx])
        else:
            final_trips.append(trip)
    final_trips.extend(pickups)

    # ── Rebuild day list, replacing RSV days that received a callout ─────
    final_days: list[Day] = []
    for day in baseline.days:
        if day.date is not None and day.date in callout_pch_by_date:
            final_days.append(
                replace(
                    day,
                    callout_trip_pch=callout_pch_by_date[day.date],
                    callout_trip_id=callout_aid_by_date.get(day.date),
                )
            )
        else:
            final_days.append(day)

    updated_month = replace(
        baseline,
        trips=tuple(final_trips),
        days=tuple(final_days),
    )
    return updated_month, tuple(events)


# ── Helpers ─────────────────────────────────────────────────────────────


def _is_reserve_day(day: Day) -> bool:
    return day.duty_type.value == "RSV"


def _find_baseline_aid_for_packet_trip(
    packet_trip_id: str,
    baseline_aid_segments: list[tuple[str, tuple[str, ...]]],
) -> str | None:
    """Match a packet trip_id ("768/768/769") to a baseline aid ("768").

    The Final Award prints a short-form assignment ID per day cell while
    the packet uses the full leg sequence. A FA aid like ``"722/754"``
    refers to packet trip ``"722/723/754/755"`` — same trip, shorter label.

    Direct equality wins; otherwise we accept the aid whose flying segments
    form an ordered subsequence of the packet trip_id's segments, preferring
    the LONGEST (most specific) match so e.g. ``722/750`` beats ``722/R1``
    for packet ``722/723/750/751`` instead of the bare ``722`` claiming it.
    """
    if not packet_trip_id:
        return None
    for aid, _ in baseline_aid_segments:
        if aid == packet_trip_id:
            return aid
    packet_segments = packet_trip_id.split("/")
    best: str | None = None
    best_len = 0
    for aid, aid_segs in baseline_aid_segments:
        if (
            aid_segs
            and len(aid_segs) > best_len
            and _is_ordered_subsequence(aid_segs, packet_segments)
        ):
            best, best_len = aid, len(aid_segs)
    return best


def _next_unmatched_index_for_aid(
    aid: str | None,
    aid_to_indexes: dict[str, list[int]],
    matched_indexes: set[int],
    baseline_trips: tuple[Trip, ...],
    target_date: date_t,
) -> int | None:
    """First unmatched baseline-trip index for a given aid, or None.

    When a baseline Trip carries ``dates`` (set by the FA converter),
    prefer the index whose Trip.dates contains the reconciled trip's
    first-leg date — this prevents same-aid trips on different dates
    from claiming each other's events (e.g. a duty extension on June 17
    must update FISHER's June 17 ``722/754`` Trip, not her June 6 one).

    Falls back to first-available when no candidate carries the date
    (or none carry dates at all — synthetic / legacy Trips).
    """
    if aid is None:
        return None
    candidates = [
        idx for idx in aid_to_indexes.get(aid, ()) if idx not in matched_indexes
    ]
    if not candidates:
        return None
    for idx in candidates:
        if target_date in baseline_trips[idx].dates:
            return idx
    return candidates[0]


_RESERVE_SEG_RE = re.compile(r"^R\d+$", re.IGNORECASE)


def _flying_segments(aid: str) -> tuple[str, ...]:
    """The flight-number segments of an FA assignment id, dropping a trailing
    reserve designator.

    An FA aid like ``768/R1`` means "fly trip 768 (packet ``768/769``), then
    sit reserve" — the ``R1`` is a reserve tail, not a flight. For matching
    the flown trip we keep only the leading flight segments (``("768",)``),
    so it reconciles to the packet instead of being mistaken for an
    open-time pickup. A purely-reserve aid reduces to ``()`` and matches
    nothing.
    """
    segs = tuple(s for s in aid.split("/") if s)
    while segs and _RESERVE_SEG_RE.match(segs[-1]):
        segs = segs[:-1]
    return segs


def _is_ordered_subsequence(needle: tuple[str, ...], haystack: list[str]) -> bool:
    if not needle:
        return False
    i = 0
    for token in haystack:
        if i < len(needle) and needle[i] == token:
            i += 1
        if i == len(needle):
            return True
    return False


def _apply_duty_extension(
    baseline_trip: Trip,
    rt: ReconciledTrip,
    tolerance_hours: Decimal,
    events: list[AppliedEvent],
) -> Trip:
    """If actual block exceeds packet block by more than the tolerance,
    append an AssignmentVersion with recomputed PCH and return a new Trip;
    otherwise return the baseline Trip unchanged.

    Duty time is estimated from the leg span (first-leg start → last-leg
    end); the packet's published TAFB is reused for trip-rig recomputation
    since the iCal feed doesn't separately publish a release time. This is
    a rough recompute — sufficient to spot duty extensions of meaningful
    size. Future refinement can plumb actual show/release deltas if those
    signals become available.
    """
    packet = rt.packet_trip
    assert packet is not None  # MatchStatus.MATCHED implies packet_trip set
    actual_block = rt.actual_block_hours
    packet_block = packet.sch_block_hours

    if actual_block <= packet_block + tolerance_hours:
        return baseline_trip

    duty_span_seconds = (rt.last_dt_utc - rt.first_dt_utc).total_seconds()
    actual_duty_hours = Decimal(int(duty_span_seconds)) / Decimal("3600")
    recomputed = components_from_times(
        block_hours=actual_block,
        duty_hours=actual_duty_hours,
        tafb_hours=packet.tafb_hours,
        workdays=packet.workdays,
        deadhead=packet.deadhead_pch,
    )
    recomputed_pch = recomputed.trip_pch
    effective = effective_trip_pch_after_reassignment(
        baseline_trip.published_pch, recomputed_pch,
    )

    if effective <= baseline_trip.published_pch:
        return baseline_trip

    new_version = AssignmentVersion(
        seq=len(baseline_trip.versions) + 1,
        pch_value=recomputed_pch,
        label=(
            f"Duty extension from iCal: block {packet_block:.2f}h → "
            f"{actual_block:.2f}h"
        ),
    )
    events.append(
        AppliedEvent(
            kind=AppliedEventKind.DUTY_EXTENSION,
            date=rt.first_dt_utc.date(),
            trip_id=rt.trip_id,
            detail=(
                f"Block {packet_block:.2f}h → {actual_block:.2f}h; "
                f"recomputed PCH {recomputed_pch} (published "
                f"{baseline_trip.published_pch})"
            ),
            delta_pch=effective - baseline_trip.published_pch,
        )
    )
    return replace(
        baseline_trip,
        versions=baseline_trip.versions + (new_version,),
    )
