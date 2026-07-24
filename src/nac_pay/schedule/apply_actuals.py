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
from datetime import datetime as datetime_t
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from nac_pay.engine.constants import (
    DPG,
    REPORT_PAD_HOURS,
    TRIP_END_PAD_HOURS,
)
from nac_pay.engine.trip_pch import (
    components_from_times,
    effective_trip_pch_after_reassignment,
)
from nac_pay.parsers import OffEvent, ReconciledTrip, ReconciliationResult

# Crew domicile timezone: feed timestamps are UTC; trips are attributed to
# their Anchorage-local civil date to match the FA schedule, the reconciliation
# month-scoping, and the /day/<date> routes. Shared helper (nac_pay.timeutil)
# so the parsers/schedule/app layers can't drift.
from nac_pay.timeutil import local_date as _local_date

from .labels import EntryMode, PremiumCategory, ReasonCode
from .models import AssignmentVersion, Day, Month, Trip

_DUTY_EXTENSION_TOLERANCE_HOURS: Decimal = Decimal("0.05")  # 3 minutes


class AppliedEventKind(StrEnum):
    DUTY_EXTENSION = "DUTY_EXTENSION"
    RESERVE_CALLOUT = "RESERVE_CALLOUT"
    OPEN_TIME_PICKUP = "OPEN_TIME_PICKUP"
    UNMATCHED_TRIP_REVIEW = "UNMATCHED_TRIP_REVIEW"
    FEED_REASSIGNMENT = "FEED_REASSIGNMENT"
    OFF_DAY_PICKUP = "OFF_DAY_PICKUP"
    COMPANY_CANCELLATION = "COMPANY_CANCELLATION"


# Decision states for a feed-detected reassignment. No stored decision =
# PROPOSED; the pilot confirms or rejects on the day page.
REASSIGN_PROPOSED = "PROPOSED"
REASSIGN_CONFIRMED = "CONFIRMED"
REASSIGN_REJECTED = "REJECTED"

# FeedReassignment.kind — a reroute replaces an FA-scheduled trip; an
# off-day pickup is a company-added trip on a day with no scheduled flying.
REASSIGN_KIND_REROUTE = "REROUTE"
REASSIGN_KIND_OFF_DAY_PICKUP = "OFF_DAY_PICKUP"



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


@dataclass(frozen=True)
class FeedReassignment:
    """A company mid-month reroute detected from the iCal feed.

    The feed shows a trip whose routing isn't in the packet, landing on a day
    that already carries an FA-scheduled trip — a §3.E.1.b reassignment. The
    new flight becomes the active assignment on the calendar; pay is
    ``max(original published, recomputed-from-actuals)`` so it never reduces.
    ``status`` gates the UI: PROPOSED shows a confirm badge, CONFIRMED clears
    it, REJECTED suppresses the reassignment (calendar reverts to the FA
    original) and ``applied`` is False.

    ``kind`` distinguishes a reroute of a scheduled day (REROUTE) from a
    company-added trip on a day off (OFF_DAY_PICKUP): for a pickup there is
    no published value to protect, so ``original_aid`` is ``"OFF"``,
    ``original_pch`` is 0, and ``effective_pch`` is the credited
    recompute/override (0 when rejected — the day stays OFF).
    """

    date: date_t
    signature: str          # new flight sequence, e.g. "730/730/731"
    original_aid: str       # the FA-scheduled trip id it replaces
    original_pch: Decimal
    new_pch: Decimal        # recomputed from actuals (TAFB borrowed from original)
    effective_pch: Decimal  # what the day pays = max(original, new/override) (or original if rejected)
    status: str             # PROPOSED | CONFIRMED | REJECTED
    applied: bool           # False when REJECTED
    override_pch: Decimal | None = None  # pilot-entered company PCH, if any
    kind: str = REASSIGN_KIND_REROUTE    # REROUTE | OFF_DAY_PICKUP


def apply_actuals_to_month(
    baseline: Month,
    reconciliation: ReconciliationResult,
    *,
    duty_extension_tolerance_hours: Decimal = _DUTY_EXTENSION_TOLERANCE_HOURS,
    packet: dict | None = None,
    feed_reassignment_decisions: dict[tuple[str, str], str] | None = None,
    feed_reassignment_pch_overrides: dict[tuple[str, str], Decimal] | None = None,
) -> tuple[Month, tuple[AppliedEvent, ...], tuple[FeedReassignment, ...]]:
    """Apply mid-month events from a reconciliation onto a baseline Month.

    Returns the updated Month, a tuple of AppliedEvent records, and a tuple of
    FeedReassignment records (company reroutes detected from the feed).

    ``packet`` (the parsed Trip Pairing Packet, keyed by trip_id) lets a
    feed-detected reassignment borrow the original trip's TAFB / workdays /
    deadhead when recomputing PCH for the new routing. ``feed_reassignment_
    decisions`` maps ``(date_iso, signature)`` → ``"CONFIRMED"``/``"REJECTED"``
    (absence = PROPOSED); a REJECTED reassignment is suppressed.
    ``feed_reassignment_pch_overrides`` maps ``(date_iso, signature)`` → a
    pilot-entered company PCH that replaces the recomputed value (still
    protected: the day pays ``max(published, override)``).
    """
    decisions = feed_reassignment_decisions or {}
    pch_overrides = feed_reassignment_pch_overrides or {}
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
    callout_published_by_date: dict[date_t, Decimal] = {}
    callout_aid_by_date: dict[date_t, str] = {}
    events: list[AppliedEvent] = []

    # ── Matched reconciled trips ─────────────────────────────────────────
    for rt in reconciliation.matched:
        # Anchorage-local date (not UTC) — disambiguates same-aid-on-different-
        # dates and keys callout dates on the civil day the pilot flew, matching
        # the FA schedule and the day view (see _local_date).
        first_date = _local_date(rt.first_dt_utc)
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
            # A callout is a protected trip — auto-credit the greater of the
            # published value and the §3.E recompute from actual times (a long
            # callout / duty extension), same as a matched trip gets.
            recomputed = _recomputed_actual_pch(rt)
            callout_pch = rt.published_pch
            if (
                recomputed is not None
                and recomputed > rt.published_pch + duty_extension_tolerance_hours
            ):
                callout_pch = recomputed
            callout_pch_by_date[first_date] = callout_pch
            callout_published_by_date[first_date] = rt.published_pch
            callout_aid_by_date[first_date] = rt.trip_id
            excess = max(Decimal("0"), callout_pch - DPG)
            extended_note = (
                f" (recomputed from actuals, published {rt.published_pch:.2f})"
                if callout_pch != rt.published_pch else ""
            )
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.RESERVE_CALLOUT,
                    date=first_date,
                    trip_id=rt.trip_id,
                    detail=(
                        f"Reserve callout to {rt.trip_id} "
                        f"(pch={callout_pch:.2f}){extended_note}"
                    ),
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

    # ── Unmatched reconciled trips ───────────────────────────────────────
    # An unmatched feed trip that lands on a day already carrying an
    # FA-scheduled Trip is a COMPANY REASSIGNMENT / reroute (§3.E.1.b): the
    # new routing isn't in the packet, but the day was scheduled. Auto-apply
    # it as an AssignmentVersion (new flight active, pay max(original, new)),
    # gated by the pilot's confirm/reject decision. Anything else (no
    # scheduled trip on that date) stays a log-only review item.
    reassign_version_by_index: dict[int, AssignmentVersion] = {}
    consumed_for_reassign: set[int] = set()
    feed_reassignments: list[FeedReassignment] = []
    for rt in reconciliation.unmatched:
        # Attribute the reroute by its Anchorage-LOCAL date, not UTC. An
        # AK-evening departure (out ~18:00 local) is already the next calendar
        # day in UTC, so a UTC date looks for the FA-scheduled trip on the
        # wrong day, finds none, and silently drops the reassignment to a
        # log-only review — this exact bug hid the July 6 732/732/733 reroute
        # (out 02:00 UTC = 18:00 AKDT Jul 6). first_date also keys the decision
        # store and must match the /day/<date_iso> confirm/reject routes.
        first_date = _local_date(rt.first_dt_utc)
        idx = _baseline_trip_index_for_date(
            baseline.trips, first_date, matched_indexes | consumed_for_reassign,
        )
        if idx is None:
            if first_date in baseline_rsv_by_date:
                # Reserve day — the callout path (matched loop) and the manual
                # ⚡ flow own these; an unmatched sequence here stays a review
                # item rather than guessing at callout pay.
                events.append(
                    AppliedEvent(
                        kind=AppliedEventKind.UNMATCHED_TRIP_REVIEW,
                        date=first_date,
                        trip_id=None,
                        detail=(
                            f"Flew sequence {rt.flight_sequence} ({len(rt.legs)} legs, "
                            f"actual_block={rt.actual_block_hours:.2f}h) — not in packet; "
                            "needs pilot categorization (charter? non-bid reassignment?)"
                        ),
                        delta_pch=None,
                    )
                )
                continue

            # Company-added trip on a day with NO scheduled flying (OFF /
            # leave): surface it like a reassignment — auto-credit, badge for
            # confirm/reject — instead of burying it in a log event (the
            # 2026-07-23 2720/2721 callout was invisible exactly this way).
            # Pay is the §3.E recompute from actuals, which DPG-floors at
            # 3.82; premium stays 1.0× until the pilot sets it (premiums are
            # never preassigned — the pilot picks the category on the day
            # page).
            signature = rt.flight_sequence
            new_pch = _recomputed_reroute_pch(rt, None)
            decision = decisions.get((first_date.isoformat(), signature))

            if decision == REASSIGN_REJECTED:
                feed_reassignments.append(
                    FeedReassignment(
                        date=first_date, signature=signature,
                        original_aid="OFF", original_pch=Decimal("0"),
                        new_pch=new_pch, effective_pch=Decimal("0"),
                        status=REASSIGN_REJECTED, applied=False,
                        kind=REASSIGN_KIND_OFF_DAY_PICKUP,
                    )
                )
                events.append(
                    AppliedEvent(
                        kind=AppliedEventKind.OFF_DAY_PICKUP,
                        date=first_date, trip_id=signature,
                        detail=(
                            f"Off-day pickup {signature} rejected — "
                            "day remains OFF"
                        ),
                        delta_pch=None,
                    )
                )
                continue

            status = decision if decision == REASSIGN_CONFIRMED else REASSIGN_PROPOSED
            override = pch_overrides.get((first_date.isoformat(), signature))
            credited = override if override is not None else new_pch
            pickups.append(
                Trip(
                    trip_id=signature,
                    published_pch=credited,
                    reason_code=ReasonCode.FLOWN,
                    premium_category=PremiumCategory.OPEN_TIME_BID_PERIOD,
                    workdays=rt.calendar_days_touched,
                    entry_mode=EntryMode.SIMPLE,
                    label=(
                        f"Company pickup {signature} on "
                        f"{first_date.isoformat()} (feed, day off)"
                    ),
                    dates=(first_date,),
                )
            )
            feed_reassignments.append(
                FeedReassignment(
                    date=first_date, signature=signature,
                    original_aid="OFF", original_pch=Decimal("0"),
                    new_pch=new_pch, effective_pch=credited,
                    status=status, applied=True, override_pch=override,
                    kind=REASSIGN_KIND_OFF_DAY_PICKUP,
                )
            )
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.OFF_DAY_PICKUP,
                    date=first_date, trip_id=signature,
                    detail=(
                        f"Company-added trip {signature} on a day off "
                        + (
                            f"(company PCH {override:.2f}"
                            if override is not None
                            else f"(recomputed {new_pch:.2f}"
                        )
                        + f"); crediting {credited:.2f} at 1.0× — set the "
                        "premium on the day page if it qualifies"
                        + (" — confirmed" if status == REASSIGN_CONFIRMED
                           else " — needs confirmation")
                    ),
                    delta_pch=credited,
                )
            )
            continue

        consumed_for_reassign.add(idx)
        baseline_trip = baseline.trips[idx]
        signature = rt.flight_sequence
        original_packet = (
            packet_trip_for_aid(baseline_trip.trip_id, packet)
            if packet else None
        )
        new_pch = _recomputed_reroute_pch(rt, original_packet)
        decision = decisions.get((first_date.isoformat(), signature))

        if decision == REASSIGN_REJECTED:
            feed_reassignments.append(
                FeedReassignment(
                    date=first_date, signature=signature,
                    original_aid=baseline_trip.trip_id,
                    original_pch=baseline_trip.published_pch,
                    new_pch=new_pch,
                    effective_pch=baseline_trip.published_pch,
                    status=REASSIGN_REJECTED, applied=False,
                )
            )
            events.append(
                AppliedEvent(
                    kind=AppliedEventKind.FEED_REASSIGNMENT,
                    date=first_date, trip_id=signature,
                    detail=(
                        f"Company reassignment {signature} rejected — showing "
                        f"Final Award {baseline_trip.trip_id}"
                    ),
                    delta_pch=None,
                )
            )
            continue

        status = decision if decision == REASSIGN_CONFIRMED else REASSIGN_PROPOSED
        # A pilot-entered company PCH (CONFIRMED only) replaces the recomputed
        # value as the reassignment's asserted worth — the company sometimes
        # assigns a PCH the feed can't express. Still protected: pay the
        # greater of published and the credited (override-or-recomputed) value.
        override = pch_overrides.get((first_date.isoformat(), signature))
        credited = override if override is not None else new_pch
        effective = max(baseline_trip.published_pch, credited)
        reassign_version_by_index[idx] = AssignmentVersion(
            seq=0,  # real seq assigned during rebuild (after any duty extension)
            pch_value=credited,
            label=(
                f"Company reassignment (feed): {signature}"
                + (" · company PCH" if override is not None else "")
            ),
        )
        feed_reassignments.append(
            FeedReassignment(
                date=first_date, signature=signature,
                original_aid=baseline_trip.trip_id,
                original_pch=baseline_trip.published_pch,
                new_pch=new_pch, effective_pch=effective,
                status=status, applied=True,
                override_pch=override,
            )
        )
        events.append(
            AppliedEvent(
                kind=AppliedEventKind.FEED_REASSIGNMENT,
                date=first_date, trip_id=signature,
                detail=(
                    f"Company reassignment to {signature} "
                    + (
                        f"(company PCH {override:.2f} vs published "
                        if override is not None
                        else f"(recomputed {new_pch:.2f} vs published "
                    )
                    + f"{baseline_trip.published_pch:.2f}); paying {effective:.2f}"
                    + (" — confirmed" if status == REASSIGN_CONFIRMED
                       else " — needs confirmation")
                ),
                delta_pch=effective - baseline_trip.published_pch,
            )
        )

    # ── Rebuild trip list preserving baseline order ──────────────────────
    final_trips: list[Trip] = []
    for idx, trip in enumerate(baseline.trips):
        t = duty_extension_by_index.get(idx, trip)
        if idx in reassign_version_by_index:
            version = replace(
                reassign_version_by_index[idx], seq=len(t.versions) + 1,
            )
            t = replace(t, versions=t.versions + (version,))
        final_trips.append(t)
    final_trips.extend(pickups)

    # ── Rebuild day list, replacing RSV days that received a callout ─────
    final_days: list[Day] = []
    for day in baseline.days:
        if day.date is not None and day.date in callout_pch_by_date:
            final_days.append(
                replace(
                    day,
                    callout_trip_pch=callout_pch_by_date[day.date],
                    callout_published_pch=callout_published_by_date.get(day.date),
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
    return updated_month, tuple(events), tuple(feed_reassignments)


# ── Helpers ─────────────────────────────────────────────────────────────


# The feed's affirmative pay-protected-cancellation marker. BlueOne cancels
# a trip by REMOVING its FLT legs and posting an all-day leave event in their
# place (seen live 2026-07-15: ``LEA - OFF/PAY PROTECTED`` replaced 768/R1).
# Leg ABSENCE alone must never be read as cancellation — completed legs also
# roll off the feed (see parsers.ical_merge) — so only this explicit label
# flips a scheduled trip to the cancelled state.
_PAY_PROTECTED_MARKER = "PAY PROTECTED"


def apply_feed_cancellations(
    month: Month,
    off_days: tuple[OffEvent, ...],
) -> tuple[Month, tuple[AppliedEvent, ...]]:
    """Mark FA-scheduled trips the company cancelled with pay protection.

    An ``LEA`` event whose label contains ``PAY PROTECTED`` landing on a
    date that carries a scheduled Trip means the company cancelled that
    day's flying and the pilot keeps the published PCH. Pay is unchanged
    (the engine already credits the published value); this stamps the Trip
    ``cancelled_pay_protected`` for the calendar/day views and logs a
    ``COMPANY_CANCELLATION`` event.

    Run this AFTER the other month transforms (user versions, overrides)
    so a rebuild can't strip the flag.
    """
    events: list[AppliedEvent] = []
    cancelled_dates: set[date_t] = set()
    for ev in off_days:
        if _PAY_PROTECTED_MARKER in ev.label.upper():
            cancelled_dates.add(_local_date(ev.dt_start_utc))
    if not cancelled_dates:
        return month, ()

    new_trips: list[Trip] = []
    for trip in month.trips:
        hit = next((d for d in trip.dates if d in cancelled_dates), None)
        if hit is None or trip.cancelled_pay_protected:
            new_trips.append(trip)
            continue
        new_trips.append(replace(trip, cancelled_pay_protected=True))
        events.append(
            AppliedEvent(
                kind=AppliedEventKind.COMPANY_CANCELLATION,
                date=hit,
                trip_id=trip.trip_id,
                detail=(
                    f"Company cancelled {trip.trip_id} — pay protected at "
                    f"published {trip.published_pch:.2f} PCH "
                    "(feed: LEA OFF/PAY PROTECTED)"
                ),
                delta_pch=Decimal("0"),
            )
        )
    if not events:
        return month, ()
    return replace(month, trips=tuple(new_trips)), tuple(events)


def _is_reserve_day(day: Day) -> bool:
    return day.duty_type.value == "RSV"


def _baseline_trip_index_for_date(
    baseline_trips: tuple[Trip, ...],
    target_date: date_t,
    excluded: set[int],
) -> int | None:
    """First not-yet-consumed baseline Trip index whose dates include
    ``target_date``. Used to attach a feed-detected company reassignment to
    the FA-scheduled trip it replaces. ``excluded`` holds indexes already
    claimed by a matched trip or an earlier reassignment so two feed trips on
    one day don't fight over the same baseline trip."""
    for idx, trip in enumerate(baseline_trips):
        if idx in excluded:
            continue
        if target_date in trip.dates:
            return idx
    return None


def _recomputed_reroute_pch(
    rt: ReconciledTrip,
    original_packet: "TripPairing | None",
) -> Decimal:
    """§3.E PCH for a company reroute, recomputed from ACTUAL iCal times.

    Flight-op = actual block, duty-rig = padded actual duty ÷ 2. The reroute
    isn't in the packet, so trip-rig / cumulative-DPG / deadhead borrow the
    ORIGINAL trip's packet values (it's the same assignment re-routed) when
    available; otherwise TAFB falls back to the actual duty and workdays to
    the calendar days the legs touch."""
    block = rt.actual_block_hours
    duty = _actual_duty_hours(rt)
    if original_packet is not None:
        tafb = original_packet.tafb_hours
        workdays = original_packet.workdays
        deadhead = original_packet.deadhead_pch
    else:
        tafb = duty
        workdays = rt.calendar_days_touched
        deadhead = Decimal("0")
    return components_from_times(
        block_hours=block,
        duty_hours=duty,
        tafb_hours=tafb,
        workdays=workdays,
        deadhead=deadhead,
    ).trip_pch


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

    A DATED candidate on a *different* date must not be claimed: a flown
    trip whose date matches no scheduled occurrence of the aid is a
    separate event (a mid-month open-time pickup of the same pairing),
    and swallowing it here silently drops its credit — the pilot's real
    July 16 2026 pickup of ``722/723/R1`` vanished into the July 2
    baseline trip (a sick day) exactly this way. Returning None routes
    the trip to the callout / pickup paths instead.

    Only date-less candidates (synthetic / legacy Trips) fall back to
    first-available matching.
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
    for idx in candidates:
        if not baseline_trips[idx].dates:
            return idx
    return None


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


def packet_trip_for_aid(aid: str, packet: dict) -> "TripPairing | None":
    """Resolve the packet trip an FA / assignment aid refers to, by ordered
    subsequence of '/'-segments — the same matching the feed reconciliation
    uses, but driven by the assignment id alone. Works with NO iCal legs, so
    the day view can reconstruct a packet duty window once the feed has aged
    out. Exact key wins; else the LONGEST (most specific) packet trip whose
    segments contain the aid's flying segments in order. None for no match or
    a purely-reserve aid (e.g. a bare reserve line)."""
    if not aid:
        return None
    if aid in packet:
        return packet[aid]
    aid_segs = _flying_segments(aid)
    if not aid_segs:
        return None
    best = None
    best_len = -1
    for key, tp in packet.items():
        key_segs = key.split("/")
        if _is_ordered_subsequence(aid_segs, key_segs) and len(key_segs) > best_len:
            best, best_len = tp, len(key_segs)
    return best


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


def _actual_duty_hours(rt: ReconciledTrip) -> Decimal:
    """The ACTUAL duty period from iCal, padded to report→release so it's
    comparable to the packet's already-padded scheduled duty: report
    REPORT_PAD before the first leg out, release TRIP_END_PAD after the last
    leg in (§3.E; same padding the day-detail duty window uses)."""
    duty_start = rt.first_dt_utc - timedelta(hours=float(REPORT_PAD_HOURS))
    duty_end = rt.last_dt_utc + timedelta(hours=float(TRIP_END_PAD_HOURS))
    seconds = int((duty_end - duty_start).total_seconds())
    return Decimal(seconds) / Decimal("3600")


def _recomputed_actual_pch(rt: ReconciledTrip) -> Decimal | None:
    """§3.E PCH recomputed from ACTUAL iCal times — the greater of actual
    flight-op (block) and actual duty rig (padded duty ÷ 2), plus trip rig /
    cumulative DPG / deadhead from the packet (the feed doesn't publish TAFB
    or DH separately). None when there's no matched packet to source those.
    """
    packet = rt.packet_trip
    if packet is None:
        return None
    recomputed = components_from_times(
        block_hours=rt.actual_block_hours,
        duty_hours=_actual_duty_hours(rt),
        tafb_hours=packet.tafb_hours,
        workdays=packet.workdays,
        deadhead=packet.deadhead_pch,
    )
    return recomputed.trip_pch


def _apply_duty_extension(
    baseline_trip: Trip,
    rt: ReconciledTrip,
    tolerance_hours: Decimal,
    events: list[AppliedEvent],
) -> Trip:
    """Auto-credit a duty extension: recompute §3.E PCH from the ACTUAL iCal
    times (padded duty rig + block) and, if it beats the published value by
    more than the tolerance, append an AssignmentVersion so the engine's
    ``Trip.effective_pch`` pays ``max(published, recomputed)``. Otherwise
    return the baseline Trip unchanged.

    Triggered by either a longer actual block OR a longer actual duty (the
    rig over the report→release window) — previously only a block overrun
    triggered, which missed extensions driven by ground/duty time.
    """
    packet = rt.packet_trip
    assert packet is not None  # MatchStatus.MATCHED implies packet_trip set

    recomputed_pch = _recomputed_actual_pch(rt)
    if recomputed_pch is None:
        return baseline_trip
    if recomputed_pch <= baseline_trip.published_pch + tolerance_hours:
        return baseline_trip

    new_version = AssignmentVersion(
        seq=len(baseline_trip.versions) + 1,
        pch_value=recomputed_pch,
        label=(
            f"Duty extension from iCal: recomputed {recomputed_pch:.2f} "
            f"(block {rt.actual_block_hours:.2f}h, duty "
            f"{_actual_duty_hours(rt):.2f}h)"
        ),
    )
    events.append(
        AppliedEvent(
            kind=AppliedEventKind.DUTY_EXTENSION,
            date=_local_date(rt.first_dt_utc),
            trip_id=rt.trip_id,
            detail=(
                f"Actual block {rt.actual_block_hours:.2f}h / duty "
                f"{_actual_duty_hours(rt):.2f}h → recomputed PCH "
                f"{recomputed_pch:.2f} (published "
                f"{baseline_trip.published_pch:.2f})"
            ),
            delta_pch=recomputed_pch - baseline_trip.published_pch,
        )
    )
    return replace(
        baseline_trip,
        versions=baseline_trip.versions + (new_version,),
    )
