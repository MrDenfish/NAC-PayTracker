"""Section 3 constants and the multiplier table."""

from decimal import Decimal

MPG: Decimal = Decimal("65")
DPG: Decimal = Decimal("3.82")
TRIP_RIG_DIVISOR: Decimal = Decimal("4.90")
PCH_DP: int = 2

# Duty-window padding used to derive duty rig from iCal *actual* leg times,
# which carry no report/release allowance. Report (show) before the first
# leg's departure; trip-end pad after the final block-in.
#
# VERIFIED against JCBA §13.C.1 "Duty On and Release Times" (printed
# p.148-149) on 2026-08-20:
# - Report is 60 minutes before scheduled departure in EVERY air case —
#   in/out of domicile, international, and deadheading — so
#   REPORT_PAD_HOURS = 1.0 is exact. (§13.C.2 lets the company move DOT
#   earlier for airline/government check-in requirements; not modeled.)
# - Release is TIERED: 15 min in-domicile operating and domestic
#   deadhead; 30 min out-of-domicile operating and international
#   deadhead; 45 min international operating; surface deadhead pads
#   neither end ("actual departure"/"actual arrival").
#   TRIP_END_PAD_HOURS = 0.25 is exact for the in-domicile / domestic-DH
#   cases and UNDERSTATES the rest — a duty window ending out-of-domicile
#   is 15 min short (0.125 PCH of duty rig). Tiering the release pad by
#   arrival station + international flag is an open item.
# §7.C.2 makes these pads part of a paid Deadhead Assignment.
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
