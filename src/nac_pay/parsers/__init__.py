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

__all__ = [
    "DayCell",
    "PilotMonthSchedule",
    "parse_master_schedule",
]
