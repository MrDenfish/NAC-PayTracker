"""Domicile (Anchorage) timezone helpers.

Feed timestamps are UTC; the Final Award, packet, and every /day route key
on the pilot's Anchorage-local civil date. This is the single home for that
conversion so the parsers, schedule, and app layers can't drift (the July 6
and July 23 2026 incidents were both UTC-vs-local date-attribution bugs).

NOTE: assumes an ANC domicile. A non-ANC base would need a profile-driven
timezone — tracked as an open item, deliberately not built yet.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DOMICILE_TZ = ZoneInfo("America/Anchorage")


def local_date(dt: datetime) -> date:
    """Anchorage-local civil date of an aware (UTC) timestamp."""
    return dt.astimezone(DOMICILE_TZ).date()


def scheduled_report_utc(
    clock_hhmm: str, first_out_utc: datetime,
) -> datetime | None:
    """Resolve the packet's local "L Day Show" clock to a UTC instant.

    The packet prints the show time as a bare local ``"HH:MM"`` with no
    date, so it is anchored to the trip's first departure: same local day,
    stepped back one day when the clock lands *after* that departure (an
    evening show for a trip whose first leg pushes past local midnight).

    Returns None for an unparsable or missing clock — callers fall back to
    their own estimate rather than inventing a report time.
    """
    try:
        hh, mm = (int(part) for part in clock_hhmm.split(":"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    local_out = first_out_utc.astimezone(DOMICILE_TZ)
    report = local_out.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if report > local_out:
        report -= timedelta(days=1)
    return report.astimezone(timezone.utc)
