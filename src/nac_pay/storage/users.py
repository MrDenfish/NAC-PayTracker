"""User identity layer — SQL-backed.

Phase 2 of the multi-tenant refactor. ``UserStore`` now reads/writes the
``users`` table via SQLAlchemy. The default user resolves even when the
table is empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

DEFAULT_USER_ID = "default"


@dataclass(frozen=True)
class User:
    user_id: str
    email: str = ""
    created_at: str = ""
    is_default: bool = False


_DEFAULT_USER = User(
    user_id=DEFAULT_USER_ID,
    email="",
    is_default=True,
)


def default_user() -> User:
    return _DEFAULT_USER


def user_dir(base_dir: Path, user_id: str) -> Path:
    """Legacy helper kept for the Phase 1 storage tests (path namespacing
    test). The SQL backend uses a single DB file; this function only
    returns the conceptual per-user dir under base_dir."""
    return base_dir / "users" / user_id


class UserStore:
    def __init__(self, base_dir: Path | None = None):
        # base_dir is accepted for API compatibility but not used —
        # the DB URL is resolved by db.database_url().
        pass

    def list_users(self) -> list[User]:
        from .db import session_scope
        from .db_models import UserRow

        with session_scope() as sess:
            rows = sess.execute(select(UserRow)).scalars().all()
            out = [
                User(
                    user_id=r.user_id, email=r.email,
                    created_at=r.created_at, is_default=r.is_default,
                )
                for r in rows
            ]
        # Default user always resolves, even if not persisted yet.
        if not any(u.user_id == DEFAULT_USER_ID for u in out):
            out.append(default_user())
        return out

    def get(self, user_id: str) -> User | None:
        from .db import session_scope
        from .db_models import UserRow

        with session_scope() as sess:
            row = sess.execute(
                select(UserRow).where(UserRow.user_id == user_id)
            ).scalar_one_or_none()
            if row is None:
                # Default user resolves even if not in the table.
                if user_id == DEFAULT_USER_ID:
                    return default_user()
                return None
            return User(
                user_id=row.user_id, email=row.email,
                created_at=row.created_at, is_default=row.is_default,
            )

    def upsert(self, user: User) -> None:
        from .db import session_scope
        from .db_models import UserRow

        with session_scope() as sess:
            row = sess.execute(
                select(UserRow).where(UserRow.user_id == user.user_id)
            ).scalar_one_or_none()
            if row is None:
                row = UserRow(user_id=user.user_id)
                sess.add(row)
            row.email = user.email
            row.created_at = user.created_at
            row.is_default = user.is_default

    def reset(self) -> None:
        """Clear all users (and cascade-delete their profiles + overrides)."""
        from .db import session_scope
        from .db_models import UserRow
        from sqlalchemy import delete as sa_delete

        with session_scope() as sess:
            sess.execute(sa_delete(UserRow))
