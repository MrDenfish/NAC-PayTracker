"""Pilot confirm/reject decisions on feed-detected reassignments — SQL store.

A company mid-month reroute is auto-applied by the pipeline as a §3.E.1.b
reassignment but stays PROPOSED until the pilot confirms or rejects it. Only
that decision is persisted here; the reassignment itself is re-derived from
the iCal feed on every pipeline run (see ``schedule.apply_actuals``). So the
absence of a row = PROPOSED, ``CONFIRMED`` = keep the new assignment,
``REJECTED`` = suppress it and show the Final Award original.

Keyed by ``(user_id, date_iso, signature)`` where signature is the new flight
sequence (e.g. ``"730/730/731"``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

STATUS_CONFIRMED = "CONFIRMED"
STATUS_REJECTED = "REJECTED"


class FeedReassignmentDecisionStore:
    def __init__(self, base_dir: Path | None = None, user_id: str | None = None):
        from .users import DEFAULT_USER_ID
        self._user_id = user_id or DEFAULT_USER_ID

    def decisions_for_month(self, year: int, month: int) -> dict[tuple[str, str], str]:
        """Return ``{(date_iso, signature): status}`` for the given month."""
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow

        prefix = f"{year:04d}-{month:02d}-"
        with session_scope() as sess:
            rows = sess.execute(
                select(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id,
                    FeedReassignmentDecisionRow.date_iso.startswith(prefix),
                )
            ).scalars().all()
            return {(r.date_iso, r.signature): r.status for r in rows}

    def get(self, date_iso: str, signature: str) -> str | None:
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow

        with session_scope() as sess:
            row = sess.execute(
                select(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id,
                    FeedReassignmentDecisionRow.date_iso == date_iso,
                    FeedReassignmentDecisionRow.signature == signature,
                )
            ).scalar_one_or_none()
            return row.status if row is not None else None

    def set(self, date_iso: str, signature: str, status: str) -> None:
        """Upsert a CONFIRMED/REJECTED decision."""
        if status not in (STATUS_CONFIRMED, STATUS_REJECTED):
            raise ValueError(f"Invalid decision status {status!r}")
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow, UserRow

        with session_scope() as sess:
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                sess.add(UserRow(user_id=self._user_id))
                sess.flush()

            row = sess.execute(
                select(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id,
                    FeedReassignmentDecisionRow.date_iso == date_iso,
                    FeedReassignmentDecisionRow.signature == signature,
                )
            ).scalar_one_or_none()
            if row is None:
                row = FeedReassignmentDecisionRow(
                    user_id=self._user_id,
                    date_iso=date_iso,
                    signature=signature,
                )
                sess.add(row)
            row.status = status
            row.decided_at = datetime.now(timezone.utc).isoformat()

    def clear(self, date_iso: str, signature: str) -> None:
        """Remove a decision, reverting the reassignment to PROPOSED."""
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow

        with session_scope() as sess:
            sess.execute(
                delete(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id,
                    FeedReassignmentDecisionRow.date_iso == date_iso,
                    FeedReassignmentDecisionRow.signature == signature,
                )
            )

    def reset(self) -> None:
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow

        with session_scope() as sess:
            sess.execute(
                delete(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id
                )
            )
