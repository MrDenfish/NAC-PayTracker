"""Input parsers — Master Schedule (Final Awards), Trip Pairing Packet, iCal feed.

Each parser is a thin function from file path → structured data, with no
domain-model assumptions baked in. Higher layers (or callers) translate
the parser output into ``schedule.Month`` etc.
"""

from .master_schedule import (
    DayCell,
    PilotMonthSchedule,
    parse_master_schedule,
)
from .trip_pairing_packet import (
    TripPairing,
    parse_trip_pairing_packet,
)
from .validation import (
    ValidationDiscrepancy,
    validate_trip_pairing_packet,
)

__all__ = [
    "DayCell",
    "PilotMonthSchedule",
    "TripPairing",
    "ValidationDiscrepancy",
    "parse_master_schedule",
    "parse_trip_pairing_packet",
    "validate_trip_pairing_packet",
]
