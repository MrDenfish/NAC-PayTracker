"""Pilot confirm/reject decisions on feed-detected reassignments — SQL store.

A company mid-month reroute is auto-applied by the pipeline as a §3.E.1.b
reassignment but stays PROPOSED until the pilot confirms or rejects it. Only
that decision is persisted here; the reassignment itself is re-derived from
the iCal feed on every pipeline run (see ``schedule.apply_actuals``). So the
absence of a row = PROPOSED, ``CONFIRMED`` = keep the new assignment,
``REJECTED`` = suppress it and show the Final Award original.

Keyed by ``(user_id, date_iso, signature)`` where signature is the new flight
sequence (e.g. ``"730/730/731"``).

A CONFIRMED decision may also carry a pilot-entered ``pch_value`` — the
company sometimes assigns a PCH the iCal feed can't express — applied as
``max(published, this)`` so it never reduces pay.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

    def pch_overrides_for_month(
        self, year: int, month: int
    ) -> dict[tuple[str, str], Decimal]:
        """Return ``{(date_iso, signature): Decimal}`` for decisions in the
        month that carry a pilot-entered PCH override (skips NULL/blank)."""
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow

        prefix = f"{year:04d}-{month:02d}-"
        out: dict[tuple[str, str], Decimal] = {}
        with session_scope() as sess:
            rows = sess.execute(
                select(FeedReassignmentDecisionRow).where(
                    FeedReassignmentDecisionRow.user_id == self._user_id,
                    FeedReassignmentDecisionRow.date_iso.startswith(prefix),
                )
            ).scalars().all()
            for r in rows:
                if not r.pch_value:
                    continue
                try:
                    out[(r.date_iso, r.signature)] = Decimal(r.pch_value)
                except InvalidOperation:
                    continue
        return out

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

    def set(
        self,
        date_iso: str,
        signature: str,
        status: str,
        pch: Decimal | None = None,
    ) -> None:
        """Upsert a CONFIRMED/REJECTED decision.

        ``pch`` is an optional pilot-entered PCH override (CONFIRMED only) —
        stored as a decimal string, ignored (cleared) on REJECT."""
        if status not in (STATUS_CONFIRMED, STATUS_REJECTED):
            raise ValueError(f"Invalid decision status {status!r}")
        if pch is not None and (not isinstance(pch, Decimal) or pch <= 0):
            raise ValueError(f"Invalid PCH override {pch!r}")
        from .db import session_scope
        from .db_models import FeedReassignmentDecisionRow, UserRow

        # A PCH override only makes sense on a kept (CONFIRMED) reassignment.
        pch_str = str(pch) if (pch is not None and status == STATUS_CONFIRMED) else None

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
            row.pch_value = pch_str
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
