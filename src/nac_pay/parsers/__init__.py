"""Input parsers — Master Schedule (Final Awards), Trip Pairing Packet, iCal feed.

Each parser is a thin function from file path → structured data, with no
domain-model assumptions baked in. Higher layers (or callers) translate
the parser output into ``schedule.Month`` etc.
"""

from .ical_feed import (
    FlightLegEvent,
    OffEvent,
    ParsedFeed,
    ReserveEvent,
    UnknownEvent,
    parse_ical_feed,
)
from .master_schedule import (
    DayCell,
    PilotMonthSchedule,
    parse_master_schedule,
)
from .pay_stub import (
    PayStub,
    PayStubLine,
    parse_pay_stub,
)
from .reconciliation import (
    MatchStatus,
    ReconciledTrip,
    ReconciliationResult,
    reconcile_feed_to_packet,
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
    "FlightLegEvent",
    "MatchStatus",
    "OffEvent",
    "ParsedFeed",
    "PayStub",
    "PayStubLine",
    "PilotMonthSchedule",
    "ReconciledTrip",
    "ReconciliationResult",
    "ReserveEvent",
    "TripPairing",
    "UnknownEvent",
    "ValidationDiscrepancy",
    "parse_ical_feed",
    "parse_master_schedule",
    "parse_pay_stub",
    "parse_trip_pairing_packet",
    "reconcile_feed_to_packet",
    "validate_trip_pairing_packet",
]
