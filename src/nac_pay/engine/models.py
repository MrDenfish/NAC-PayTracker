"""Engine vocabulary: Chunk, FloorEvent, EngineInput, EngineResult.

The engine reasons in *chunks* (credited units of PCH) and *floor events*
(mutations to the adjusted-MPG floor). It does not know about trips, days,
legs, pilots, calendars, or pay stubs — that's the schedule layer's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class ChunkKind(StrEnum):
    TRIP = "TRIP"
    RESERVE_DAY = "RESERVE_DAY"
    TRAINING = "TRAINING"
    PTO = "PTO"
    SICK = "SICK"
    JURY = "JURY"
    BEREAVEMENT = "BEREAVEMENT"
    MOVING = "MOVING"
    MILITARY = "MILITARY"
    OPEN_TIME = "OPEN_TIME"
    NRFO = "NRFO"
    HOME_STUDY = "HOME_STUDY"
    OTHER = "OTHER"


class FloorEventKind(StrEnum):
    OPEN_TIME_PICKUP = "OPEN_TIME_PICKUP"
    INVOLUNTARY_EXCESS = "INVOLUNTARY_EXCESS"
    VOLUNTARY_DROP = "VOLUNTARY_DROP"
    LESSER_TRADE = "LESSER_TRADE"
    UNPROTECTED_UNAVAIL = "UNPROTECTED_UNAVAIL"


class WinningOption(StrEnum):
    FLOOR = "floor"
    WORKDAYS_DPG = "workdays_dpg"
    EARNED = "earned"


@dataclass(frozen=True)
class Chunk:
    """One unit of credit going through the engine.

    raw_pch counts toward the guarantee (Option 3) and toward Stage 2 dollars.
    multiplier applies to dollars only.

    ``premium_category`` is the source Trip/Day's ``PremiumCategory.value``
    when applicable, blank otherwise. Carried so the display layer can
    categorize premium rows correctly — `ChunkKind` alone is not enough
    because the same kind (TRIP / OTHER) can apply at 1.0× or with a
    premium multiplier depending on Overtime / Landing / Junior etc.
    """

    source_id: str
    kind: ChunkKind
    raw_pch: Decimal
    multiplier: Decimal = Decimal("1.0")
    rate_override: Decimal | None = None
    in_guarantee: bool = True
    workdays: int = 0
    label: str = ""
    premium_category: str = ""
    # The amount this chunk contributes to the *forfeitable* floor base
    # (Option 1's drop-cap "remaining"). Defaults to raw_pch. A reserve
    # callout sets this to DPG: its base credit is the daily guarantee, while
    # the involuntary excess (raw_pch − DPG) rides ON TOP as a protected
    # FloorEvent — so counting raw_pch here too would double-count the excess
    # when a voluntary drop forces the floor down to "remaining".
    floor_base_pch: Decimal | None = None


@dataclass(frozen=True)
class FloorEvent:
    """One mutation of Option 1 (the adjusted-MPG floor).

    seq orders events; the engine doesn't need real datetimes. delta_pch is
    the magnitude before kind-specific rules are applied (e.g., for
    INVOLUNTARY_EXCESS this is the *excess over DPG*, already computed).
    """

    seq: int
    kind: FloorEventKind
    delta_pch: Decimal
    label: str = ""


@dataclass(frozen=True)
class EngineInput:
    line_value: Decimal
    hourly_rate: Decimal
    chunks: tuple[Chunk, ...] = ()
    floor_events: tuple[FloorEvent, ...] = ()


@dataclass(frozen=True)
class ChunkResult:
    source_id: str
    kind: ChunkKind
    raw_pch: Decimal
    multiplier: Decimal
    rate: Decimal
    dollars: Decimal
    label: str = ""
    premium_category: str = ""


@dataclass(frozen=True)
class EngineResult:
    base_monthly_pch: Decimal
    winning_option: WinningOption

    option1_floor: Decimal
    option2_workdays_dpg: Decimal
    option3_earned: Decimal

    earned_dollars: Decimal
    topup_pch: Decimal
    topup_dollars: Decimal
    total_pay: Decimal

    per_chunk: tuple[ChunkResult, ...] = field(default_factory=tuple)
