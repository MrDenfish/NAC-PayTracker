"""Apply per-date pilot overrides onto a Month.

Sits between ``apply_actuals_to_month`` (which pulls events from iCal +
packet reconciliation) and ``lower_month`` (which compiles for the
engine). Pilot edits made in the GUI land here.

Override schema (see ``nac_pay.storage.DayOverride``):
- ``reason_code``       — replaces ``Trip.reason_code`` / ``Day.reason_code``
- ``premium_category``  — replaces ``.premium_category`` (Trip only — Day
                          doesn't currently carry one in the engine path)
- ``custom_multiplier`` — replaces ``.custom_multiplier`` (used with
                          ``PremiumCategory.CUSTOM``)
- ``entry_mode``        — replaces ``Trip.entry_mode``

Match rule: an override with date X applies to whichever Trip's
``dates`` contains X *or* whichever Day's ``date`` equals X. If both
exist on the same date (unusual), the Trip wins because that's what the
day detail page presents first.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date as date_t
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from .labels import EntryMode, PremiumCategory, ReasonCode
from .models import Day, Month, Trip

if TYPE_CHECKING:
    from nac_pay.storage import DayOverride


def apply_overrides_to_month(
    month: Month,
    overrides: dict[str, "DayOverride"],
) -> Month:
    if not overrides:
        return month

    consumed: set[str] = set()
    new_trips: list[Trip] = []
    for trip in month.trips:
        ov = _override_for_trip(trip, overrides)
        if ov is None:
            new_trips.append(trip)
            continue
        consumed.add(ov.date_iso)
        new_trips.append(_apply_to_trip(trip, ov))

    new_days: list[Day] = []
    for day in month.days:
        if day.date is None:
            new_days.append(day)
            continue
        ov = overrides.get(day.date.isoformat())
        if ov is None or ov.date_iso in consumed:
            new_days.append(day)
            continue
        new_days.append(_apply_to_day(day, ov))

    return replace(month, trips=tuple(new_trips), days=tuple(new_days))


def _override_for_trip(
    trip: Trip,
    overrides: dict[str, "DayOverride"],
) -> "DayOverride | None":
    for d in trip.dates:
        ov = overrides.get(d.isoformat())
        if ov is not None:
            return ov
    return None


def _apply_to_trip(trip: Trip, ov: "DayOverride") -> Trip:
    fields: dict = {}
    if ov.reason_code:
        try:
            fields["reason_code"] = ReasonCode(ov.reason_code)
        except ValueError:
            pass
    if ov.premium_category:
        try:
            fields["premium_category"] = PremiumCategory(ov.premium_category)
        except ValueError:
            pass
    if ov.custom_multiplier:
        try:
            fields["custom_multiplier"] = Decimal(ov.custom_multiplier)
        except (InvalidOperation, ValueError):
            pass
    if ov.entry_mode:
        try:
            fields["entry_mode"] = EntryMode(ov.entry_mode)
        except ValueError:
            pass
    return replace(trip, **fields) if fields else trip


def _apply_to_day(day: Day, ov: "DayOverride") -> Day:
    fields: dict = {}
    if ov.reason_code:
        try:
            fields["reason_code"] = ReasonCode(ov.reason_code)
        except ValueError:
            pass
    if ov.premium_category:
        try:
            fields["premium_category"] = PremiumCategory(ov.premium_category)
        except ValueError:
            pass
    if ov.custom_multiplier:
        try:
            fields["custom_multiplier"] = Decimal(ov.custom_multiplier)
        except (InvalidOperation, ValueError):
            pass
    return replace(day, **fields) if fields else day
