"""Pilot-editable label families that drive the engine (§4, §7).

Two parallel taxonomies:

- ``ReasonCode`` — *why* a scheduled trip wasn't flown (controls whether
  published PCH is kept and whether the floor stays protected).
- ``PremiumCategory`` — *does this pay at a premium* (controls the
  multiplier applied at the dollar stage).

Both default from the inputs (Final Award duty type, packet TRIP PCH
VALUE) and are pilot-editable. The engine knows nothing about either
family — they're translated by the lowering step into the engine's raw
``Chunk`` / ``FloorEvent`` vocabulary.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from nac_pay.engine.constants import (
    PREMIUM_HOSTILE,
    PREMIUM_JA_FIRST,
    PREMIUM_JA_SECOND_PLUS,
    PREMIUM_LANDING,
    PREMIUM_NRFO_SPECIALIZED,
    PREMIUM_OPEN_TIME,
    PREMIUM_OPEN_TIME_BID_PERIOD,
    PREMIUM_OVERTIME,
    REGULAR_MULT,
)


class Position(StrEnum):
    FO = "FO"
    CPT = "CPT"


class DutyType(StrEnum):
    """The duty-type row from the Master Schedule day cell (§10)."""

    FLT = "FLT"            # flight trip
    RSV = "RSV"            # reserve
    PTO = "PTO"            # vacation
    FMLA = "FMLA"          # FMLA (unpaid)
    CLASS = "CLASS"        # classroom training
    SIM = "SIM"            # simulator
    DH = "DH"              # deadhead
    VX = "VX"              # leave / X-out
    OFF = "OFF"            # day off
    # Stubs we'll flesh out later:
    TAXI = "TAXI"          # §3.M (stubbed)
    HOME_STUDY = "HOME_STUDY"   # §3.H
    MOVING = "MOVING"      # §3.K


class EntryMode(StrEnum):
    """Dual-entry mode (§3 data sources)."""

    SIMPLE = "SIMPLE"        # pilot entered the PCH value directly
    DETAILED = "DETAILED"    # actual times entered; engine computes


class PremiumScope(StrEnum):
    TRIP = "TRIP"
    LEG = "LEG"
    DUTY_PERIOD = "DUTY_PERIOD"


class ReasonCode(StrEnum):
    """Why a scheduled trip / day didn't fly as a normal flown trip.

    Default is FLOWN. Effect on chunk + floor is in REASON_EFFECTS below.
    """

    FLOWN = "FLOWN"
    PTO = "PTO"
    SICK = "SICK"
    JURY = "JURY"
    BEREAVEMENT = "BEREAVEMENT"
    TRAINING = "TRAINING"
    MOVING = "MOVING"
    FAR = "FAR"
    MILITARY = "MILITARY"           # stubbed — see §12 open question
    FMLA = "FMLA"
    UNPAID_LOA = "UNPAID_LOA"
    VOLUNTARY_DROP = "VOLUNTARY_DROP"
    LESSER_TRADE = "LESSER_TRADE"
    UNPROTECTED_UNAVAIL = "UNPROTECTED_UNAVAIL"
    OFF = "OFF"


class ReasonEffect(StrEnum):
    """How a reason code lowers into chunks + floor events.

    KEEP_PROTECTED:    chunk at published PCH, no floor event (PTO, SICK,
                       JURY, BEREAVEMENT, TRAINING, MOVING, FAR).
    FLOWN_DEFAULT:     chunk at effective PCH with the premium multiplier
                       (the normal case).
    ZERO_PCH:          no chunk emitted; day contributes 0 (FMLA, UNPAID_LOA,
                       OFF).
    FLOOR_DROP:        no chunk; emits a drop-type floor event
                       (VOLUNTARY_DROP, LESSER_TRADE, UNPROTECTED_UNAVAIL).
    MILITARY_STUB:     not implemented — open question §12. Lowering will
                       raise rather than guess.
    """

    KEEP_PROTECTED = "KEEP_PROTECTED"
    FLOWN_DEFAULT = "FLOWN_DEFAULT"
    ZERO_PCH = "ZERO_PCH"
    FLOOR_DROP = "FLOOR_DROP"
    MILITARY_STUB = "MILITARY_STUB"


REASON_EFFECTS: dict[ReasonCode, ReasonEffect] = {
    ReasonCode.FLOWN: ReasonEffect.FLOWN_DEFAULT,
    ReasonCode.PTO: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.SICK: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.JURY: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.BEREAVEMENT: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.TRAINING: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.MOVING: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.FAR: ReasonEffect.KEEP_PROTECTED,
    ReasonCode.MILITARY: ReasonEffect.MILITARY_STUB,
    ReasonCode.FMLA: ReasonEffect.ZERO_PCH,
    ReasonCode.UNPAID_LOA: ReasonEffect.ZERO_PCH,
    ReasonCode.OFF: ReasonEffect.ZERO_PCH,
    ReasonCode.VOLUNTARY_DROP: ReasonEffect.FLOOR_DROP,
    ReasonCode.LESSER_TRADE: ReasonEffect.FLOOR_DROP,
    ReasonCode.UNPROTECTED_UNAVAIL: ReasonEffect.FLOOR_DROP,
}


class PremiumCategory(StrEnum):
    """The pilot picks a category; the engine looks up the multiplier."""

    NONE = "NONE"
    OPEN_TIME_MID_MONTH = "OPEN_TIME_MID_MONTH"     # 1.5×, P.2
    OPEN_TIME_BID_PERIOD = "OPEN_TIME_BID_PERIOD"   # 1.0×, P.1
    OVERTIME = "OVERTIME"                            # 1.5×, Q
    JUNIOR_ASSIGNMENT_1ST = "JUNIOR_ASSIGNMENT_1ST"  # 2.0×, R.1
    JUNIOR_ASSIGNMENT_NTH = "JUNIOR_ASSIGNMENT_NTH"  # 2.5×, R.2
    LANDING = "LANDING"                              # 1.5×, T (leg scope)
    HOSTILE = "HOSTILE"                              # 2.0×, U (duty-period scope)
    NRFO_SPECIALIZED = "NRFO_SPECIALIZED"            # 1.5×, L.2
    CUSTOM = "CUSTOM"                                # pilot-defined


PREMIUM_MULTIPLIERS: dict[PremiumCategory, Decimal] = {
    PremiumCategory.NONE: REGULAR_MULT,
    PremiumCategory.OPEN_TIME_MID_MONTH: PREMIUM_OPEN_TIME,
    PremiumCategory.OPEN_TIME_BID_PERIOD: PREMIUM_OPEN_TIME_BID_PERIOD,
    PremiumCategory.OVERTIME: PREMIUM_OVERTIME,
    PremiumCategory.JUNIOR_ASSIGNMENT_1ST: PREMIUM_JA_FIRST,
    PremiumCategory.JUNIOR_ASSIGNMENT_NTH: PREMIUM_JA_SECOND_PLUS,
    PremiumCategory.LANDING: PREMIUM_LANDING,
    PremiumCategory.HOSTILE: PREMIUM_HOSTILE,
    PremiumCategory.NRFO_SPECIALIZED: PREMIUM_NRFO_SPECIALIZED,
    # CUSTOM is intentionally absent — the pilot supplies the multiplier
    # directly on the chunk/trip; lowering pulls it from the explicit field.
}

# Premium categories whose chunk pairs with an OPEN_TIME_PICKUP floor event
# (the chunk shows up in option3; the event lifts the floor on top).
PREMIUM_IS_OPEN_TIME_PICKUP: frozenset[PremiumCategory] = frozenset(
    {
        PremiumCategory.OPEN_TIME_MID_MONTH,
        PremiumCategory.OPEN_TIME_BID_PERIOD,
    }
)


def premium_multiplier(
    category: PremiumCategory,
    custom_multiplier: Decimal | None = None,
) -> Decimal:
    """Resolve a premium category to its multiplier.

    For CUSTOM, ``custom_multiplier`` must be supplied — the engine's contract
    is that the pilot picks the *type*, not the percentage, except in the
    custom-override path.
    """
    if category is PremiumCategory.CUSTOM:
        if custom_multiplier is None:
            raise ValueError("CUSTOM premium requires an explicit custom_multiplier")
        return custom_multiplier
    return PREMIUM_MULTIPLIERS[category]
