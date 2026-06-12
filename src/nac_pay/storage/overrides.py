"""Per-date pilot overrides — SQL-backed store.

Same public API as the Phase 1 JSON store.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select


@dataclass(frozen=True)
class DayOverride:
    date_iso: str
    reason_code: str | None = None
    premium_category: str | None = None
    custom_multiplier: str | None = None
    entry_mode: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any((
            self.reason_code,
            self.premium_category,
            self.custom_multiplier,
            self.entry_mode,
        ))

    @property
    def custom_multiplier_decimal(self) -> Decimal | None:
        return Decimal(self.custom_multiplier) if self.custom_multiplier else None


class DayOverrideStore:
    def __init__(self, base_dir: Path | None = None, user_id: str | None = None):
        from .users import DEFAULT_USER_ID
        self._user_id = user_id or DEFAULT_USER_ID

    def load_all(self) -> dict[str, DayOverride]:
        from .db import session_scope
        from .db_models import DayOverrideRow

        with session_scope() as sess:
            rows = sess.execute(
                select(DayOverrideRow).where(
                    DayOverrideRow.user_id == self._user_id
                )
            ).scalars().all()
            return {
                r.date_iso: DayOverride(
                    date_iso=r.date_iso,
                    reason_code=r.reason_code,
                    premium_category=r.premium_category,
                    custom_multiplier=(
                        str(r.custom_multiplier)
                        if r.custom_multiplier is not None
                        else None
                    ),
                    entry_mode=r.entry_mode,
                )
                for r in rows
            }

    def save_one(self, override: DayOverride) -> None:
        from .db import session_scope
        from .db_models import DayOverrideRow, UserRow

        with session_scope() as sess:
            # Ensure user exists (the per-user PK FK requires it).
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                sess.add(UserRow(user_id=self._user_id))
                sess.flush()

            if override.is_empty:
                sess.execute(
                    delete(DayOverrideRow).where(
                        DayOverrideRow.user_id == self._user_id,
                        DayOverrideRow.date_iso == override.date_iso,
                    )
                )
                return

            row = sess.execute(
                select(DayOverrideRow).where(
                    DayOverrideRow.user_id == self._user_id,
                    DayOverrideRow.date_iso == override.date_iso,
                )
            ).scalar_one_or_none()
            if row is None:
                row = DayOverrideRow(
                    user_id=self._user_id,
                    date_iso=override.date_iso,
                )
                sess.add(row)
            row.reason_code = override.reason_code
            row.premium_category = override.premium_category
            row.custom_multiplier = (
                Decimal(override.custom_multiplier)
                if override.custom_multiplier else None
            )
            row.entry_mode = override.entry_mode

    def delete_one(self, date_iso: str) -> None:
        from .db import session_scope
        from .db_models import DayOverrideRow

        with session_scope() as sess:
            sess.execute(
                delete(DayOverrideRow).where(
                    DayOverrideRow.user_id == self._user_id,
                    DayOverrideRow.date_iso == date_iso,
                )
            )

    def reset(self) -> None:
        from .db import session_scope
        from .db_models import DayOverrideRow

        with session_scope() as sess:
            sess.execute(
                delete(DayOverrideRow).where(
                    DayOverrideRow.user_id == self._user_id
                )
            )
