"""Section 3 constants and the multiplier table."""

from decimal import Decimal

MPG: Decimal = Decimal("65")
DPG: Decimal = Decimal("3.82")
TRIP_RIG_DIVISOR: Decimal = Decimal("4.90")
PCH_DP: int = 2

REGULAR_MULT: Decimal = Decimal("1.0")
PREMIUM_OPEN_TIME: Decimal = Decimal("1.5")
PREMIUM_OPEN_TIME_BID_PERIOD: Decimal = Decimal("1.0")
PREMIUM_OVERTIME: Decimal = Decimal("1.5")
PREMIUM_JA_FIRST: Decimal = Decimal("2.0")
PREMIUM_JA_SECOND_PLUS: Decimal = Decimal("2.5")
PREMIUM_LANDING: Decimal = Decimal("1.5")
PREMIUM_HOSTILE: Decimal = Decimal("2.0")
PREMIUM_NRFO_SPECIALIZED: Decimal = Decimal("1.5")
