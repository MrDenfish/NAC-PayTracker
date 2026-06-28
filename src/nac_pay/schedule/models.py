"""Schedule-layer domain model.

This is what the pilot sees and edits. The engine knows nothing about
these types; the lowering step (``lower.py``) translates them into the
engine's ``Chunk`` / ``FloorEvent`` vocabulary.

Decimal throughout. Frozen dataclasses so equality / hashing are by value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_t
from decimal import Decimal

from nac_pay.engine.trip_pch import effective_trip_pch_after_reassignment

from .labels import (
    DutyType,
    EntryMode,
    Position,
    PremiumCategory,
    PremiumScope,
    ReasonCode,
)


@dataclass(frozen=True)
class PilotProfile:
    pilot_id: str
    name: str
    position: Position
    hourly_rate: Decimal
    fleet: str = "737"            # NAC operates the 737 only
    sick_bank_days: int = 0
    pto_bank_days: int = 0


@dataclass(frozen=True)
class Leg:
    flight_no: str
    origin: str
    destination: str
    tail: str = ""
    sch_block: Decimal = Decimal("0")
    actual_block: Decimal | None = None
    has_landing: bool = True
    is_excess_landing: bool = False   # set later by the leg-scoped landing pass


@dataclass(frozen=True)
class AssignmentVersion:
    """One snapshot of a trip's assignment.

    The Trip carries a tuple of these; effective PCH = max across all per
    §3.E.1.b (see ``Trip.effective_pch``). seq=0 is the original.
    """

    seq: int
    pch_value: Decimal
    legs: tuple[Leg, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class Trip:
    """A multi-day trip pairing.

    ``published_pch`` is the original packet value (TRIP PCH VALUE).
    ``versions`` holds mid-month revisions (reassignments, reroutes, duty
    extensions). The pilot is paid the high-water mark across the chain
    (§3.E.1.b), exposed as ``effective_pch``.
    """

    trip_id: str
    published_pch: Decimal
    versions: tuple[AssignmentVersion, ...] = ()
    reason_code: ReasonCode = ReasonCode.FLOWN
    premium_category: PremiumCategory = PremiumCategory.NONE
    premium_scope: PremiumScope = PremiumScope.TRIP
    entry_mode: EntryMode = EntryMode.SIMPLE
    workdays: int = 0
    custom_multiplier: Decimal | None = None   # required iff premium is CUSTOM
    label: str = ""
    dates: tuple[date_t, ...] = ()
    """Calendar dates this trip occupies. Populated by the FA converter from
    the day-cell date; used by apply_actuals to disambiguate when the same
    aid is scheduled on multiple dates in a month (e.g. FISHER's ``"722/754"``
    on both June 6 and June 17). Empty tuple for legacy/synthetic Trips —
    those fall back to first-available matching in apply_actuals."""

    @property
    def effective_pch(self) -> Decimal:
        return effective_trip_pch_after_reassignment(
            self.published_pch,
            *(v.pch_value for v in self.versions),
        )


@dataclass(frozen=True)
class Day:
    """A single-day, non-trip item: reserve, PTO, training, sick, off, etc.

    Per §3.D.2 each Day represents one duty period (or none, for OFF).
    Multi-day items belong on a ``Trip`` instead.

    ``callout_trip_pch`` is set on a reserve day that received a callout —
    lowering treats the day as a flown trip rather than a sit-reserve, and
    emits the ``INVOLUNTARY_EXCESS`` floor event for any excess over DPG.
    """

    date: date_t | None
    duty_type: DutyType
    pch_value: Decimal = Decimal("0")
    reason_code: ReasonCode = ReasonCode.FLOWN
    premium_category: PremiumCategory = PremiumCategory.NONE
    workdays: int = 1
    callout_trip_pch: Decimal | None = None
    # The callout trip's PUBLISHED packet value, kept separate from
    # callout_trip_pch (which is the *credited* value = greater of published
    # and the §3.E recompute from actuals). Lets the day card show the true
    # published alongside the actual duty-rig/block candidates. None for older
    # data / when unknown.
    callout_published_pch: Decimal | None = None
    # The flown trip id a reserve callout was assigned to (e.g. "720/1780"),
    # captured from the iCal reconciliation. Surfaced on the calendar as the
    # bold "new assignment" over the subtle reserve line. None when unknown.
    callout_trip_id: str | None = None
    custom_multiplier: Decimal | None = None
    label: str = ""
    # The day's PCH before any pilot reassignment version lifted/synthesized
    # it (Phase H). Preserved so the day-detail assignment history can show
    # the pre-pickup "Original published" baseline (0 for a picked-up OFF
    # day). None when the day was never touched by a pilot version.
    original_pch: Decimal | None = None


@dataclass(frozen=True)
class Month:
    """One month of schedule for one pilot.

    ``line_value`` comes from the Master Schedule (Final Award) — the
    authoritative guarantee input. ``trips`` and ``days`` together
    describe what was scheduled, picked up, or absent. Lowering produces
    one engine input from a Month.
    """

    pilot: PilotProfile
    year: int
    month: int
    line_value: Decimal
    trips: tuple[Trip, ...] = field(default_factory=tuple)
    days: tuple[Day, ...] = field(default_factory=tuple)
