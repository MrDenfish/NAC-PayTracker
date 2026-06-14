"""Persistence layer — SQL backing store (Phase 2).

Per-user data lives in three tables: ``users``, ``pilot_profiles``,
``day_overrides``. Database URL resolves from ``NAC_PAY_DATABASE_URL``
(prod) or falls back to ``sqlite:///{NAC_PAY_DATA_DIR}/nac_pay.db`` (dev).

The Phase 1 JSON store is no longer used — ``JsonStore`` remains as a
compatibility export so any out-of-tree code that imported it for tests
still gets a working class, but the production stores all hit SQL now.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path.home() / ".nac-pay" / "data"


def get_data_dir() -> Path:
    """Resolve the data directory each call so tests can monkeypatch via env."""
    override = os.environ.get("NAC_PAY_DATA_DIR")
    return Path(override) if override else DEFAULT_DATA_DIR


@dataclass(frozen=True)
class JsonStore:
    """Legacy JSON-file backing store. Kept as an export for back-compat
    only — production stores in Phase 2 use SQLAlchemy via ``db.py``."""

    path: Path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


from .db import (  # noqa: E402
    Base,
    database_url,
    dispose_engine,
    get_engine,
    reset_tables,
    session_factory,
    session_scope,
)
from .documents import (  # noqa: E402
    DocumentKind,
    DocumentRecord,
    UserDocumentsStore,
    expected_extension,
)
from .overrides import DayOverride, DayOverrideStore  # noqa: E402
from .profile import PersistedPilotProfile, PilotProfileStore  # noqa: E402
from .users import (  # noqa: E402
    DEFAULT_USER_ID,
    User,
    UserStore,
    default_user,
    user_dir,
)

__all__ = [
    "Base",
    "DEFAULT_DATA_DIR",
    "DEFAULT_USER_ID",
    "DayOverride",
    "DayOverrideStore",
    "DocumentKind",
    "DocumentRecord",
    "JsonStore",
    "PersistedPilotProfile",
    "PilotProfileStore",
    "User",
    "UserDocumentsStore",
    "UserStore",
    "database_url",
    "default_user",
    "dispose_engine",
    "expected_extension",
    "get_data_dir",
    "get_engine",
    "reset_tables",
    "session_factory",
    "session_scope",
    "user_dir",
]
