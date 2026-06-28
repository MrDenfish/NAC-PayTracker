"""Section 3 constants and the multiplier table."""

from decimal import Decimal

MPG: Decimal = Decimal("65")
DPG: Decimal = Decimal("3.82")
TRIP_RIG_DIVISOR: Decimal = Decimal("4.90")
PCH_DP: int = 2

# Duty-window padding used to derive duty rig from iCal *actual* leg times,
# which carry no report/release allowance. Report (show) before the first
# leg's departure; trip-end pad after the final block-in. Contractual values
# — NOT verified against the JCBA text (the §-citations are spec shorthand),
# so they live here as editable constants.
REPORT_PAD_HOURS: Decimal = Decimal("1.0")
TRIP_END_PAD_HOURS: Decimal = Decimal("0.25")

REGULAR_MULT: Decimal = Decimal("1.0")
PREMIUM_OPEN_TIME: Decimal = Decimal("1.5")
PREMIUM_OPEN_TIME_BID_PERIOD: Decimal = Decimal("1.0")
PREMIUM_OVERTIME: Decimal = Decimal("1.5")
PREMIUM_JA_FIRST: Decimal = Decimal("2.0")
PREMIUM_JA_SECOND_PLUS: Decimal = Decimal("2.5")
PREMIUM_LANDING: Decimal = Decimal("1.5")
PREMIUM_HOSTILE: Decimal = Decimal("2.0")
PREMIUM_NRFO_SPECIALIZED: Decimal = Decimal("1.5")
