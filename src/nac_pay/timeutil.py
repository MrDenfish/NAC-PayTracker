"""Domicile (Anchorage) timezone helpers.

Feed timestamps are UTC; the Final Award, packet, and every /day route key
on the pilot's Anchorage-local civil date. This is the single home for that
conversion so the parsers, schedule, and app layers can't drift (the July 6
and July 23 2026 incidents were both UTC-vs-local date-attribution bugs).

NOTE: assumes an ANC domicile. A non-ANC base would need a profile-driven
timezone — tracked as an open item, deliberately not built yet.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

DOMICILE_TZ = ZoneInfo("America/Anchorage")


def local_date(dt: datetime) -> date:
    """Anchorage-local civil date of an aware (UTC) timestamp."""
    return dt.astimezone(DOMICILE_TZ).date()
