"""Pilot decisions on feed-detected company drops — SQL store.

An approved mid-month drop arrives in the iCal feed as an all-day
``LEA - TRIP DROP`` event (the day's FLT legs and R/S window are removed).
The pipeline proposes the drop but keeps paying the published value until
the pilot confirms — forfeiting pay is never automatic. Confirming files a
normal ``VersionType.DROP`` user-version (the manual drop flow's own path),
so the durable state here is mostly REJECTED: the proposal itself is
re-derived from the feed on every pipeline run, and the absence of a row
means PROPOSED.

Keyed by ``(user_id, date_iso)`` — one drop decision per day.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from .feed_reassignments import STATUS_CONFIRMED, STATUS_REJECTED


class FeedDropDecisionStore:
    def __init__(self, base_dir: Path | None = None, user_id: str | None = None):
        from .users import DEFAULT_USER_ID
        self._user_id = user_id or DEFAULT_USER_ID

    def rejected_dates_for_month(self, year: int, month: int) -> set[str]:
        """ISO dates in the month whose feed-drop proposal the pilot rejected."""
        from .db import session_scope
        from .db_models import FeedDropDecisionRow

        prefix = f"{year:04d}-{month:02d}-"
        with session_scope() as sess:
            rows = sess.execute(
                select(FeedDropDecisionRow).where(
                    FeedDropDecisionRow.user_id == self._user_id,
                    FeedDropDecisionRow.date_iso.startswith(prefix),
                    FeedDropDecisionRow.status == STATUS_REJECTED,
                )
            ).scalars().all()
            return {r.date_iso for r in rows}

    def get(self, date_iso: str) -> str | None:
        from .db import session_scope
        from .db_models import FeedDropDecisionRow

        with session_scope() as sess:
            row = sess.execute(
                select(FeedDropDecisionRow).where(
                    FeedDropDecisionRow.user_id == self._user_id,
                    FeedDropDecisionRow.date_iso == date_iso,
                )
            ).scalar_one_or_none()
            return row.status if row is not None else None

    def set(self, date_iso: str, status: str) -> None:
        """Upsert a CONFIRMED/REJECTED decision for a feed-detected drop."""
        if status not in (STATUS_CONFIRMED, STATUS_REJECTED):
            raise ValueError(f"Invalid decision status {status!r}")
        from .db import session_scope
        from .db_models import FeedDropDecisionRow, UserRow

        with session_scope() as sess:
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                sess.add(UserRow(user_id=self._user_id))
                sess.flush()

            row = sess.execute(
                select(FeedDropDecisionRow).where(
                    FeedDropDecisionRow.user_id == self._user_id,
                    FeedDropDecisionRow.date_iso == date_iso,
                )
            ).scalar_one_or_none()
            if row is None:
                row = FeedDropDecisionRow(
                    user_id=self._user_id, date_iso=date_iso,
                )
                sess.add(row)
            row.status = status
            row.decided_at = datetime.now(timezone.utc).isoformat()

    def clear(self, date_iso: str) -> None:
        """Remove a decision, reverting the proposal to PROPOSED."""
        from .db import session_scope
        from .db_models import FeedDropDecisionRow

        with session_scope() as sess:
            sess.execute(
                delete(FeedDropDecisionRow).where(
                    FeedDropDecisionRow.user_id == self._user_id,
                    FeedDropDecisionRow.date_iso == date_iso,
                )
            )
