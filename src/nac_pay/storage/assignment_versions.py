"""Per-day pilot-recorded assignment versions — reassignments + corrections.

Append-only: every saved row is permanent. A CORRECTION row references a
prior seq via ``correction_of``; the engine's max-PCH comparison
(§3.E.1.b) then excludes the superseded row but the audit trail stays
intact.

The high-level flow:

    pilot_view  →  POST /day/<date>/reassign  →  UserAssignmentVersionStore.save
        ↓
    services._pipeline → load_for_month → fold into Trip.versions
        ↓
    Trip.effective_pch = max over non-superseded versions
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select


class VersionType(StrEnum):
    REASSIGNMENT = "REASSIGNMENT"
    CORRECTION = "CORRECTION"
    # A reassignment the pilot was *called in* to fly during their reserve
    # window. Pays identically to a REASSIGNMENT (the engine folds it via the
    # same max-PCH path); the distinct type only drives the ⚡ calendar marker
    # and the "Reserve callout" history label. NB: unlike the iCal-derived
    # callout (which sets Day.callout_trip_pch and gets the §3.F on-top excess
    # floor), this manual path is a plain reassignment lift — see
    # apply_user_versions for why.
    RESERVE_CALLOUT = "RESERVE_CALLOUT"
    # A company-approved DROP of a scheduled assignment. Unlike every other
    # version type (which lift pay via the §3.E.1.b max), a DROP is the
    # inverse: it removes the assignment entirely. apply_user_versions stamps
    # the matched Trip/Day with ``ReasonCode.VOLUNTARY_DROP`` so the existing
    # engine path (lower.py FLOOR_DROP) credits 0 PCH, drops the workday, and
    # forfeits the floor 1:1 by the lost PCH (§3.D). pch_value is always 0;
    # the row's existence implies company approval (the route gates the save
    # behind a required checkbox). Reverse a drop by superseding it with a
    # CORRECTION — the active-versions resolver then ignores it.
    DROP = "DROP"


class VersionEntryMode(StrEnum):
    SIMPLE = "SIMPLE"
    DETAILED = "DETAILED"


@dataclass(frozen=True)
class UserAssignmentVersion:
    """One pilot-recorded version for a date.

    Storage carries everything the form submitted (so a future
    "correct this" pre-fill is exact). The engine only needs
    ``pch_value`` and ``superseded`` (derived at read time)."""

    user_id: str
    date_iso: str
    seq: int
    version_type: VersionType
    correction_of: int | None
    assignment_id: str
    entry_mode: VersionEntryMode
    pch_value: Decimal
    block_hours: Decimal | None
    duty_hours: Decimal | None
    tafb_hours: Decimal | None
    deadhead_pch: Decimal | None
    workdays: int | None
    reason_code: str
    premium_category: str
    notes: str
    created_at: str


class UserAssignmentVersionStore:
    """Per-user, per-date assignment-version log.

    Append-only on save (history is preserved; supersession is resolved at
    read time). The one exception is ``delete`` — a pilot can hard-remove an
    erroneous entry (typo / duplicate) outright, which cascades to its
    corrections so the log never references a deleted seq."""

    def __init__(self, base_dir: Path | None = None, user_id: str | None = None):
        from .users import DEFAULT_USER_ID
        self._user_id = user_id or DEFAULT_USER_ID

    # ── Write ──────────────────────────────────────────────────────

    def save(
        self,
        *,
        date_iso: str,
        version_type: VersionType,
        assignment_id: str,
        entry_mode: VersionEntryMode,
        pch_value: Decimal,
        correction_of: int | None = None,
        block_hours: Decimal | None = None,
        duty_hours: Decimal | None = None,
        tafb_hours: Decimal | None = None,
        deadhead_pch: Decimal | None = None,
        workdays: int | None = None,
        reason_code: str = "FLOWN",
        premium_category: str = "NONE",
        notes: str = "",
    ) -> UserAssignmentVersion:
        """Append a new version. Returns the persisted record with its
        auto-assigned seq."""
        from .db import session_scope
        from .db_models import UserAssignmentVersionRow, UserRow

        with session_scope() as sess:
            # Ensure the user row exists (default user has none).
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                sess.add(UserRow(user_id=self._user_id))
                sess.flush()

            # Next seq = max existing + 1 for this (user, date).
            used = sess.execute(
                select(UserAssignmentVersionRow.seq).where(
                    UserAssignmentVersionRow.user_id == self._user_id,
                    UserAssignmentVersionRow.date_iso == date_iso,
                )
            ).scalars().all()
            seq = (max(used) + 1) if used else 1
            # seq=0 is reserved for the trip's "Original" (the published
            # value from the packet/FA) — user entries start at 1.

            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

            row = UserAssignmentVersionRow(
                user_id=self._user_id,
                date_iso=date_iso,
                seq=seq,
                version_type=version_type.value,
                correction_of=correction_of,
                assignment_id=assignment_id,
                entry_mode=entry_mode.value,
                pch_value=pch_value,
                block_hours=block_hours,
                duty_hours=duty_hours,
                tafb_hours=tafb_hours,
                deadhead_pch=deadhead_pch,
                workdays=workdays,
                reason_code=reason_code,
                premium_category=premium_category,
                notes=notes,
                created_at=created_at,
            )
            sess.add(row)

        return UserAssignmentVersion(
            user_id=self._user_id, date_iso=date_iso, seq=seq,
            version_type=version_type, correction_of=correction_of,
            assignment_id=assignment_id, entry_mode=entry_mode,
            pch_value=pch_value,
            block_hours=block_hours, duty_hours=duty_hours,
            tafb_hours=tafb_hours, deadhead_pch=deadhead_pch,
            workdays=workdays,
            reason_code=reason_code, premium_category=premium_category,
            notes=notes, created_at=created_at,
        )

    # ── Read ───────────────────────────────────────────────────────

    def list_for_date(self, date_iso: str) -> list[UserAssignmentVersion]:
        from .db import session_scope
        from .db_models import UserAssignmentVersionRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserAssignmentVersionRow).where(
                    UserAssignmentVersionRow.user_id == self._user_id,
                    UserAssignmentVersionRow.date_iso == date_iso,
                ).order_by(UserAssignmentVersionRow.seq)
            ).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def list_for_month(
        self, year: int, month: int,
    ) -> dict[str, list[UserAssignmentVersion]]:
        """All versions in a month, grouped by date_iso, each list
        ordered by seq."""
        from .db import session_scope
        from .db_models import UserAssignmentVersionRow

        prefix = f"{year:04d}-{month:02d}"
        with session_scope() as sess:
            rows = sess.execute(
                select(UserAssignmentVersionRow).where(
                    UserAssignmentVersionRow.user_id == self._user_id,
                    UserAssignmentVersionRow.date_iso.like(f"{prefix}-%"),
                ).order_by(
                    UserAssignmentVersionRow.date_iso,
                    UserAssignmentVersionRow.seq,
                )
            ).scalars().all()

        out: dict[str, list[UserAssignmentVersion]] = {}
        for r in rows:
            out.setdefault(r.date_iso, []).append(self._row_to_record(r))
        return out

    # ── Delete ─────────────────────────────────────────────────────

    def delete(self, date_iso: str, seq: int) -> list[int]:
        """Hard-delete a pilot-recorded version and cascade to any CORRECTION
        rows that (transitively) supersede it, so no correction is ever left
        pointing at a deleted seq. Returns the deleted seqs (sorted).

        This is the ONE place the version log is not append-only: it exists so
        a pilot can remove an erroneous entry (a typo or a duplicate save)
        outright rather than only superseding it. seq 0 is the synthetic
        "Original" (never a stored row), so deleting it is a no-op. Surviving
        rows keep their seq (no renumber); deleting the current top seq frees
        it for the next save, which is safe because the cascade removes any
        correction that referenced the deleted row.
        """
        from .db import session_scope
        from .db_models import UserAssignmentVersionRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserAssignmentVersionRow).where(
                    UserAssignmentVersionRow.user_id == self._user_id,
                    UserAssignmentVersionRow.date_iso == date_iso,
                )
            ).scalars().all()
            if not any(r.seq == seq for r in rows):
                return []
            # Cascade: the target plus every correction that corrects something
            # already marked for deletion (handles correction-of-a-correction
            # chains even though the UI blocks creating them).
            to_delete = {seq}
            changed = True
            while changed:
                changed = False
                for r in rows:
                    if (
                        r.version_type == VersionType.CORRECTION.value
                        and r.correction_of in to_delete
                        and r.seq not in to_delete
                    ):
                        to_delete.add(r.seq)
                        changed = True
            for r in rows:
                if r.seq in to_delete:
                    sess.delete(r)
        return sorted(to_delete)

    def _row_to_record(self, r) -> UserAssignmentVersion:
        def _dec(v) -> Decimal | None:
            return None if v is None else Decimal(str(v))
        return UserAssignmentVersion(
            user_id=r.user_id,
            date_iso=r.date_iso,
            seq=r.seq,
            version_type=VersionType(r.version_type),
            correction_of=r.correction_of,
            assignment_id=r.assignment_id,
            entry_mode=VersionEntryMode(r.entry_mode),
            pch_value=Decimal(str(r.pch_value)),
            block_hours=_dec(r.block_hours),
            duty_hours=_dec(r.duty_hours),
            tafb_hours=_dec(r.tafb_hours),
            deadhead_pch=_dec(r.deadhead_pch),
            workdays=r.workdays,
            reason_code=r.reason_code,
            premium_category=r.premium_category,
            notes=r.notes,
            created_at=r.created_at,
        )


# ── Supersession resolution ────────────────────────────────────────


def active_versions(
    versions: list[UserAssignmentVersion],
) -> tuple[list[UserAssignmentVersion], set[int]]:
    """Return ``(active, superseded_seqs)`` for one date's version list.

    A CORRECTION row supersedes its ``correction_of`` seq. Supersession
    is transitive — if v3 corrects v2 and v4 corrects v3, both v2 and v3
    are superseded. The active set is what the engine should consider in
    max-PCH; the full list is what the UI should display.

    Edge cases:
    - A correction targeting a seq that doesn't exist is treated as if
      it targets nothing (the correction is active; nothing superseded).
      The route validates this at write time, but the resolver doesn't
      depend on that for correctness.
    - A REASSIGNMENT row with a non-null ``correction_of`` is ignored
      for supersession purposes (correction semantics require the type).
    """
    by_seq = {v.seq: v for v in versions}
    superseded: set[int] = set()
    for v in versions:
        if v.version_type is VersionType.CORRECTION and v.correction_of is not None:
            if v.correction_of in by_seq:
                superseded.add(v.correction_of)
    active = [v for v in versions if v.seq not in superseded]
    return active, superseded
