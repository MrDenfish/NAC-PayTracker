"""User identity layer.

Phase 1 of the multi-tenant refactor. Currently a placeholder — the
default user is the only one that exists, and ``current_user()`` always
returns it. When auth lands in a later milestone, ``current_user()``
will read from a session cookie / JWT and routes won't need to change.

User-keyed storage layout::

    {data_dir}/
        users.json                              # registry
        users/{user_id}/
            pilot_profile.json
            day_overrides.json

For the default user we use the literal id ``"default"`` so the path is
``users/default/...`` — readable and avoids UUID churn during dev.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER_ID = "default"


@dataclass(frozen=True)
class User:
    user_id: str
    email: str = ""
    created_at: str = ""        # ISO-8601 string; populated when auth lands
    is_default: bool = False    # bundled-dev-user flag


_DEFAULT_USER = User(
    user_id=DEFAULT_USER_ID,
    email="",
    is_default=True,
)


def default_user() -> User:
    """The bundled dev/test user. Owns the docs/ corpus."""
    return _DEFAULT_USER


def user_dir(base_dir: Path, user_id: str) -> Path:
    """Per-user data directory under ``{base_dir}/users/{user_id}/``."""
    return base_dir / "users" / user_id


class UserStore:
    """Persisted user registry. Wired but minimal — the registry exists so
    we have somewhere to land sign-up flow when it ships."""

    FILENAME = "users.json"

    def __init__(self, base_dir: Path):
        from . import JsonStore
        self._store = JsonStore(base_dir / self.FILENAME)

    def list_users(self) -> list[User]:
        data = self._store.read()
        users_dict = data.get("users", {})
        out: list[User] = []
        for uid, fields in users_dict.items():
            out.append(
                User(
                    user_id=uid,
                    email=fields.get("email", ""),
                    created_at=fields.get("created_at", ""),
                    is_default=bool(fields.get("is_default", False)),
                )
            )
        # If the registry doesn't yet exist, the default user still resolves.
        if not any(u.user_id == DEFAULT_USER_ID for u in out):
            out.append(default_user())
        return out

    def get(self, user_id: str) -> User | None:
        for u in self.list_users():
            if u.user_id == user_id:
                return u
        return None

    def upsert(self, user: User) -> None:
        data = self._store.read()
        users_dict = data.get("users", {})
        users_dict[user.user_id] = {
            "email": user.email,
            "created_at": user.created_at,
            "is_default": user.is_default,
        }
        data["users"] = users_dict
        self._store.write(data)

    def reset(self) -> None:
        self._store.clear()
