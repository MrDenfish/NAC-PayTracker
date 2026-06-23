"""Persisted pilot profile — SQL-backed store.

Same public API as the Phase 1 JSON store; the backend is now SQLAlchemy.
Callers (services.py + routes) don't change. Tests still construct
``PilotProfileStore(base_dir, user_id)`` for back-compat; ``base_dir`` is
accepted but ignored (the DB URL is resolved separately from
``NAC_PAY_DATABASE_URL`` / ``NAC_PAY_DATA_DIR``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from nac_pay.schedule import PilotProfile, Position


@dataclass(frozen=True)
class PersistedPilotProfile:
    profile: PilotProfile
    feed_url: str = ""
    feed_auto_update: bool = False


class PilotProfileStore:
    def __init__(self, base_dir: Path | None = None, user_id: str | None = None):
        from .users import DEFAULT_USER_ID
        self._user_id = user_id or DEFAULT_USER_ID

    def load(self, default: PersistedPilotProfile) -> PersistedPilotProfile:
        from .db import session_scope
        from .db_models import PilotProfileRow

        with session_scope() as sess:
            row = sess.execute(
                select(PilotProfileRow).where(
                    PilotProfileRow.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if row is None:
                return default
            return PersistedPilotProfile(
                profile=PilotProfile(
                    pilot_id=row.pilot_id,
                    name=row.name,
                    position=Position(row.position),
                    hourly_rate=Decimal(str(row.hourly_rate)),
                    fleet=row.fleet,
                    sick_bank_days=row.sick_bank_days,
                    pto_bank_days=row.pto_bank_days,
                ),
                feed_url=row.feed_url,
                feed_auto_update=row.feed_auto_update,
            )

    def save(self, persisted: PersistedPilotProfile) -> None:
        from .db import session_scope
        from .db_models import PilotProfileRow, UserRow

        with session_scope() as sess:
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                user = UserRow(user_id=self._user_id)
                sess.add(user)
                sess.flush()
            row = sess.execute(
                select(PilotProfileRow).where(
                    PilotProfileRow.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if row is None:
                row = PilotProfileRow(user_id=self._user_id)
                sess.add(row)
            p = persisted.profile
            row.pilot_id = p.pilot_id
            row.name = p.name
            row.position = p.position.value
            row.fleet = p.fleet
            row.hourly_rate = p.hourly_rate
            row.sick_bank_days = p.sick_bank_days
            row.pto_bank_days = p.pto_bank_days
            row.feed_url = persisted.feed_url
            row.feed_auto_update = persisted.feed_auto_update

    def reset(self) -> None:
        from .db import session_scope
        from .db_models import PilotProfileRow

        with session_scope() as sess:
            row = sess.execute(
                select(PilotProfileRow).where(
                    PilotProfileRow.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if row is not None:
                sess.delete(row)


def feed_auto_update_profiles() -> list[tuple[str, str]]:
    """Every user who has opted into automatic feed refresh.

    Returns ``(user_id, feed_url)`` for each pilot_profiles row with
    ``feed_auto_update`` set and a non-empty ``feed_url``. The background
    feed updater iterates this set each tick. Cross-user query — not scoped
    to a single store instance — so it lives at module level."""
    from .db import session_scope
    from .db_models import PilotProfileRow

    with session_scope() as sess:
        rows = sess.execute(
            select(PilotProfileRow.user_id, PilotProfileRow.feed_url).where(
                PilotProfileRow.feed_auto_update.is_(True),
                PilotProfileRow.feed_url != "",
            )
        ).all()
    return [(r.user_id, r.feed_url) for r in rows]
