"""Lower a ``Month`` of schedule into the engine's ``EngineInput``.

The engine reasons in ``Chunk`` (credited PCH) and ``FloorEvent`` (Option 1
mutations). Lowering walks the month's trips and days and translates each
into 0+ chunks and 0+ floor events per the rules in §3 / §7.

Rules summary:

- **Reason effect** decides chunk-vs-floor-event:
  - ``FLOWN_DEFAULT``: emit chunk at effective PCH (with premium multiplier).
  - ``KEEP_PROTECTED`` (PTO/SICK/JURY/BEREAVEMENT/TRAINING/MOVING/FAR):
    emit chunk at scheduled PCH, multiplier 1.0×, no floor event.
  - ``FLOOR_DROP`` (VOLUNTARY_DROP / LESSER_TRADE / UNPROTECTED): emit no
    chunk; emit a drop-type ``FloorEvent`` at the lost PCH.
  - ``ZERO_PCH`` (FMLA / UNPAID_LOA / OFF): emit nothing.
  - ``MILITARY_STUB``: raise — §12 open question, not implemented yet.

- **Premium category** decides the chunk multiplier and, for OPEN_TIME_*,
  also emits an ``OPEN_TIME_PICKUP`` floor event so the floor lifts on top.

- **Reserve callout** (Day with ``callout_trip_pch`` set): the reserve
  day's chunk becomes a TRIP chunk at ``max(DPG, callout_pch)``; an
  ``INVOLUNTARY_EXCESS`` event adds the excess over DPG on top of the
  floor (per §3.D involuntary-assignment rule).
"""

from __future__ import annotations

from decimal import Decimal
from itertools import count

from nac_pay.engine import (
    Chunk,
    ChunkKind,
    EngineInput,
    FloorEvent,
    FloorEventKind,
)
from nac_pay.engine.constants import DPG

from .labels import (
    PREMIUM_IS_OPEN_TIME_PICKUP,
    REASON_EFFECTS,
    DutyType,
    PremiumCategory,
    ReasonCode,
    ReasonEffect,
    premium_multiplier,
)
from .models import Day, Month, Trip


def lower_month(month: Month) -> EngineInput:
    chunks: list[Chunk] = []
    events: list[FloorEvent] = []
    seq = count(1)

    for trip in month.trips:
        _lower_trip(trip, chunks, events, seq)
    for day in month.days:
        _lower_day(day, chunks, events, seq)

    return EngineInput(
        line_value=month.line_value,
        hourly_rate=month.pilot.hourly_rate,
        chunks=tuple(chunks),
        floor_events=tuple(events),
    )


# ── Trips ────────────────────────────────────────────────────────────────
def _lower_trip(
    trip: Trip,
    chunks: list[Chunk],
    events: list[FloorEvent],
    seq: count,
) -> None:
    effect = REASON_EFFECTS[trip.reason_code]

    if effect is ReasonEffect.MILITARY_STUB:
        raise NotImplementedError(
            f"Trip {trip.trip_id}: MILITARY reason code is stubbed "
            "pending §12 open-question resolution (proration method)."
        )

    if effect is ReasonEffect.ZERO_PCH:
        return

    if effect is ReasonEffect.FLOOR_DROP:
        events.append(
            FloorEvent(
                seq=next(seq),
                kind=_drop_kind_for(trip.reason_code),
                delta_pch=trip.published_pch,
                label=trip.label or f"{trip.reason_code.value} {trip.trip_id}",
            )
        )
        return

    # KEEP_PROTECTED or FLOWN_DEFAULT — emit a chunk.
    if effect is ReasonEffect.KEEP_PROTECTED:
        raw_pch = trip.published_pch
        multiplier = Decimal("1.0")
    else:
        # FLOWN_DEFAULT
        raw_pch = trip.effective_pch
        multiplier = premium_multiplier(trip.premium_category, trip.custom_multiplier)

    kind = _chunk_kind_for_trip(trip)
    chunks.append(
        Chunk(
            source_id=_trip_source_id(trip),
            kind=kind,
            raw_pch=raw_pch,
            multiplier=multiplier,
            in_guarantee=True,
            workdays=trip.workdays,
            label=trip.label,
            premium_category=trip.premium_category.value,
        )
    )

    # Open-time pickups lift the floor on top.
    if (
        effect is ReasonEffect.FLOWN_DEFAULT
        and trip.premium_category in PREMIUM_IS_OPEN_TIME_PICKUP
    ):
        events.append(
            FloorEvent(
                seq=next(seq),
                kind=FloorEventKind.OPEN_TIME_PICKUP,
                delta_pch=raw_pch,
                label=f"Open-time pickup {trip.trip_id}",
            )
        )


# ── Days ─────────────────────────────────────────────────────────────────
def _lower_day(
    day: Day,
    chunks: list[Chunk],
    events: list[FloorEvent],
    seq: count,
) -> None:
    effect = REASON_EFFECTS[day.reason_code]

    if effect is ReasonEffect.MILITARY_STUB:
        raise NotImplementedError(
            f"Day {day.date}: MILITARY reason code is stubbed "
            "pending §12 open-question resolution (proration method)."
        )

    if effect is ReasonEffect.ZERO_PCH:
        return

    if effect is ReasonEffect.FLOOR_DROP:
        events.append(
            FloorEvent(
                seq=next(seq),
                kind=_drop_kind_for(day.reason_code),
                delta_pch=day.pch_value,
                label=day.label or f"{day.reason_code.value} {day.date}",
            )
        )
        return

    if effect is ReasonEffect.KEEP_PROTECTED:
        raw_pch = day.pch_value
        multiplier = Decimal("1.0")
        kind = _chunk_kind_for_day(day)
        chunks.append(
            Chunk(
                source_id=_day_source_id(day),
                kind=kind,
                raw_pch=raw_pch,
                multiplier=multiplier,
                in_guarantee=True,
                workdays=day.workdays,
                label=day.label,
                premium_category=day.premium_category.value,
            )
        )
        return

    # FLOWN_DEFAULT — could be a reserve day, possibly with a callout.
    multiplier = premium_multiplier(day.premium_category, day.custom_multiplier)

    if day.callout_trip_pch is not None:
        # Reserve callout = a protected trip flown off reserve. Credit the
        # greater-of: DPG, the assigned/published callout value, and any pilot
        # amendment (apply_user_versions has already lifted day.pch_value to
        # max(base, active versions) — e.g. a duty-extension recompute). The
        # whole credited value is involuntary, so the excess over DPG rides
        # ON TOP of the floor, protected (Option A); the day counts at DPG in
        # the forfeitable base (floor_base_pch) so the excess isn't double
        # counted when a voluntary drop forces the floor down.
        callout_pch = day.callout_trip_pch
        raw_pch = max(DPG, callout_pch, day.pch_value)
        chunks.append(
            Chunk(
                source_id=_day_source_id(day),
                kind=ChunkKind.TRIP,
                raw_pch=raw_pch,
                multiplier=multiplier,
                in_guarantee=True,
                workdays=day.workdays,
                label=day.label or f"Reserve callout {day.date}",
                premium_category=day.premium_category.value,
                floor_base_pch=DPG,
            )
        )
        excess = max(Decimal("0"), raw_pch - DPG)
        if excess > 0:
            events.append(
                FloorEvent(
                    seq=next(seq),
                    kind=FloorEventKind.INVOLUNTARY_EXCESS,
                    delta_pch=excess,
                    label=f"Callout excess {day.date}",
                )
            )
        return

    # Normal reserve day (no callout) — or any other FLOWN day.
    kind = _chunk_kind_for_day(day)
    chunks.append(
        Chunk(
            source_id=_day_source_id(day),
            kind=kind,
            raw_pch=day.pch_value,
            multiplier=multiplier,
            in_guarantee=True,
            workdays=day.workdays,
            label=day.label,
            premium_category=day.premium_category.value,
        )
    )

    # Open-time pickup at the day level (e.g., volunteering for a day off).
    if day.premium_category in PREMIUM_IS_OPEN_TIME_PICKUP:
        events.append(
            FloorEvent(
                seq=next(seq),
                kind=FloorEventKind.OPEN_TIME_PICKUP,
                delta_pch=day.pch_value,
                label=f"Open-time pickup {day.date}",
            )
        )


# ── Helpers ──────────────────────────────────────────────────────────────
_DROP_KIND_BY_REASON: dict[ReasonCode, FloorEventKind] = {
    ReasonCode.VOLUNTARY_DROP: FloorEventKind.VOLUNTARY_DROP,
    ReasonCode.LESSER_TRADE: FloorEventKind.LESSER_TRADE,
    ReasonCode.UNPROTECTED_UNAVAIL: FloorEventKind.UNPROTECTED_UNAVAIL,
}


def _drop_kind_for(reason: ReasonCode) -> FloorEventKind:
    return _DROP_KIND_BY_REASON[reason]


def _chunk_kind_for_trip(trip: Trip) -> ChunkKind:
    if trip.premium_category in PREMIUM_IS_OPEN_TIME_PICKUP:
        return ChunkKind.OPEN_TIME
    reason_kind = _CHUNK_KIND_BY_REASON.get(trip.reason_code)
    if reason_kind is not None:
        return reason_kind
    return ChunkKind.TRIP


def _chunk_kind_for_day(day: Day) -> ChunkKind:
    if day.premium_category in PREMIUM_IS_OPEN_TIME_PICKUP:
        return ChunkKind.OPEN_TIME
    reason_kind = _CHUNK_KIND_BY_REASON.get(day.reason_code)
    if reason_kind is not None:
        return reason_kind
    return _CHUNK_KIND_BY_DUTY_TYPE.get(day.duty_type, ChunkKind.OTHER)


_CHUNK_KIND_BY_REASON: dict[ReasonCode, ChunkKind] = {
    ReasonCode.PTO: ChunkKind.PTO,
    ReasonCode.SICK: ChunkKind.SICK,
    ReasonCode.JURY: ChunkKind.JURY,
    ReasonCode.BEREAVEMENT: ChunkKind.BEREAVEMENT,
    ReasonCode.TRAINING: ChunkKind.TRAINING,
    ReasonCode.MOVING: ChunkKind.MOVING,
    # FAR is just a label; the chunk's kind comes from duty_type / TRIP default.
}


_CHUNK_KIND_BY_DUTY_TYPE: dict[DutyType, ChunkKind] = {
    DutyType.FLT: ChunkKind.TRIP,
    DutyType.RSV: ChunkKind.RESERVE_DAY,
    DutyType.PTO: ChunkKind.PTO,
    DutyType.CLASS: ChunkKind.TRAINING,
    DutyType.SIM: ChunkKind.TRAINING,
    DutyType.MOVING: ChunkKind.MOVING,
    DutyType.HOME_STUDY: ChunkKind.HOME_STUDY,
    # DH, VX, OFF, FMLA, TAXI fall through to OTHER.
}


def _trip_source_id(trip: Trip) -> str:
    """Unique chunk source_id per trip OCCURRENCE.

    A trip_id like "722/750" can appear on multiple non-contiguous dates
    in the same month (different trip pairings carrying the same label).
    Without date qualification, every chunk for "722/750" matches every
    "722/750" date — Day Pay then sums all of them per day.

    Tests build synthetic Trips without ``dates`` for engine-only unit
    tests; in that case the source_id stays as ``trip.trip_id`` for
    backward compat.
    """
    if trip.dates:
        return f"{trip.trip_id}@{trip.dates[0].isoformat()}"
    return trip.trip_id


def _day_source_id(day: Day) -> str:
    """Unique chunk source_id per day OCCURRENCE.

    A reserve day's ``label`` is the line designator (e.g. "1021"), which is
    shared by EVERY reserve day in the month. Without date qualification,
    every reserve chunk collides on one source_id, and the per-day Day Pay
    card (which filters chunks by source_id) sums the whole month's reserve
    PCH onto a single day — e.g. one reserve day showing 31.49 instead of
    3.82. Same hazard the trip path avoids; see _trip_source_id.

    Tests build synthetic Days without ``date``; those keep the bare label
    for backward compat.
    """
    if day.date is not None:
        return f"{day.label or day.duty_type.value}@{day.date.isoformat()}"
    if day.label:
        return day.label
    return f"{day.duty_type.value}"
