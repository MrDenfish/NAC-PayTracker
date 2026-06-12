"""Persistence layer — JSON file backing store.

Single-user MVP. Stores live under ``~/.nac-pay/data/`` by default; set
``NAC_PAY_DATA_DIR`` to redirect (used by tests for isolation).

Schema is intentionally minimal — pilot profile + per-date overrides.
Trip-pairing data is still re-parsed from docs/ on each pipeline run
because it's source-of-truth (the PDF). Overrides are layered on top.
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
    """Atomic JSON file: temp-then-rename so a crashed write never half-writes."""

    path: Path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Same-filesystem temp file → atomic rename.
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
    "DEFAULT_DATA_DIR",
    "DEFAULT_USER_ID",
    "DayOverride",
    "DayOverrideStore",
    "JsonStore",
    "PersistedPilotProfile",
    "PilotProfileStore",
    "User",
    "UserStore",
    "default_user",
    "get_data_dir",
    "user_dir",
]
