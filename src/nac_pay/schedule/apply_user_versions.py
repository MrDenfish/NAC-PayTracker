"""Apply pilot-recorded assignment versions onto a Month's trips.

Sits after ``apply_overrides_to_month`` and before ``lower_month``. For
each date in the user-versions map, finds the matching Trip and appends
a new ``AssignmentVersion`` per active record. The existing
``Trip.effective_pch = max(published, *versions.pch)`` then folds the
high-water mark into the rest of the engine — no engine change.

Only ACTIVE versions reach here (the caller filters superseded ones via
``nac_pay.storage.active_versions``), so the §3.E.1.b max calculation
correctly ignores a corrected typo while preserving full history for
the UI.

Matching:
- If a Trip's ``dates`` contains the version's date_iso AND its
  ``trip_id`` (or the assignment label) matches the version's
  ``assignment_id``, use that Trip.
- Otherwise, fall back to the first Trip whose ``dates`` contains the
  date_iso (single-trip days are the common case).
- A date with no Trip is currently ignored — turning a non-trip day
  (e.g. a reserve OFF) into a callout trip is not yet supported here;
  the day-detail form is hidden for non-trip days in Phase G.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date as date_t
from typing import TYPE_CHECKING

from .labels import DutyType
from .models import AssignmentVersion, Day, Month, Trip

if TYPE_CHECKING:
    from nac_pay.storage.assignment_versions import UserAssignmentVersion


def apply_user_versions_to_month(
    month: Month,
    versions_by_date: dict[str, list["UserAssignmentVersion"]],
) -> Month:
    """Fold pilot-recorded versions onto the month.

    Trip-day case: append each active version to the matching Trip's
    `versions` tuple; existing `Trip.effective_pch = max(...)` does the
    work.

    No-trip case (Phase H): for dates with no matching Trip (OFF /
    reserve sit / training / PTO), lift the matching Day's `pch_value`
    to ``max(existing_pch, max(active_versions.pch))``. The Day's
    ``duty_type`` is intentionally NOT changed — the calendar continues
    to show the Final Award assignment with a reassignment badge, and
    the day-detail screen carries the full audit. Lowering still treats
    the day as one workday but credits it at the higher PCH.
    """
    if not versions_by_date:
        return month

    consumed_dates: set[str] = set()

    new_trips: list[Trip] = []
    for trip in month.trips:
        adds = _collect_for_trip(trip, versions_by_date)
        if not adds:
            new_trips.append(trip)
            continue
        for uv in adds:
            consumed_dates.add(uv.date_iso)
        existing = trip.versions
        next_seq = max((v.seq for v in existing), default=0) + 1
        new_versions = list(existing)
        for uv in sorted(adds, key=lambda v: v.seq):
            new_versions.append(
                AssignmentVersion(
                    seq=next_seq,
                    pch_value=uv.pch_value,
                    label=_label_for(uv),
                )
            )
            next_seq += 1
        new_trips.append(replace(trip, versions=tuple(new_versions)))

    # No-trip case — two sub-cases:
    #   a) An existing Day record on the date (RSV, PTO, training, etc.):
    #      lift its pch_value to the high-water mark, keep duty_type.
    #   b) No Day record (OFF day — represented by absence in month.days):
    #      synthesize a Day with duty_type=OFF and the user's PCH so the
    #      engine pays it. duty_type stays OFF — calendar continues to
    #      show the FA-original "OFF" label with a reassignment badge.
    new_days: list = []
    for day in month.days:
        if day.date is None:
            new_days.append(day)
            continue
        iso = day.date.isoformat()
        if iso in consumed_dates:
            new_days.append(day)
            continue
        adds = versions_by_date.get(iso, ())
        if not adds:
            new_days.append(day)
            continue
        consumed_dates.add(iso)
        max_user = max(uv.pch_value for uv in adds)
        new_pch = max(day.pch_value, max_user)
        new_days.append(replace(day, pch_value=new_pch))

    # Sub-case (b): synthesize Days for dates with no Trip and no Day.
    for iso, adds in versions_by_date.items():
        if iso in consumed_dates:
            continue
        try:
            d = date_t.fromisoformat(iso)
        except ValueError:
            continue
        if d.year != month.year or d.month != month.month:
            continue
        max_user = max(uv.pch_value for uv in adds)
        new_days.append(
            Day(
                date=d,
                duty_type=DutyType.OFF,
                pch_value=max_user,
            )
        )
        consumed_dates.add(iso)

    return replace(month, trips=tuple(new_trips), days=tuple(new_days))


def _collect_for_trip(
    trip: Trip,
    versions_by_date: dict[str, list["UserAssignmentVersion"]],
) -> list["UserAssignmentVersion"]:
    """All active user-versions whose date falls in this trip's dates."""
    if not trip.dates:
        return []
    out: list = []
    for d in trip.dates:
        iso = d.isoformat()
        for uv in versions_by_date.get(iso, ()):
            # Optional refinement: if assignment_id is set AND the trip
            # has a multi-segment id, prefer matches. We keep the simple
            # "any trip on this date" rule for Phase G — the form is
            # opened from /day/<date> so the user-visible mapping is
            # date-driven already.
            out.append(uv)
    return out


def _label_for(uv: "UserAssignmentVersion") -> str:
    """Short human label for the assignment-history row."""
    from nac_pay.storage.assignment_versions import VersionType
    kind = "Correction" if uv.version_type is VersionType.CORRECTION else "Reassignment"
    if uv.assignment_id:
        return f"{kind} — {uv.assignment_id}"
    return kind
