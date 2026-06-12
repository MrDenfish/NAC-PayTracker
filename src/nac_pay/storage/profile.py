"""Persisted pilot profile + feed URL.

The schedule layer's ``PilotProfile`` is the headless engine's view of
the pilot — no UI state. This module wraps it with stub-store-style
extras (feed URL, "auto-update" toggle) that don't belong in the engine
input but do belong in the user's saved config.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from nac_pay.schedule import PilotProfile, Position


@dataclass(frozen=True)
class PersistedPilotProfile:
    """The pilot's full saved config. ``profile`` is the engine-facing
    record; ``feed_url`` + ``feed_auto_update`` are UI/runtime state we
    don't push into the engine."""

    profile: PilotProfile
    feed_url: str = ""
    feed_auto_update: bool = False


class PilotProfileStore:
    """Read / write the persisted pilot profile JSON file (per user)."""

    FILENAME = "pilot_profile.json"

    def __init__(self, base_dir: Path, user_id: str | None = None):
        from . import JsonStore
        from .users import DEFAULT_USER_ID, user_dir
        self._user_id = user_id or DEFAULT_USER_ID
        self._path = user_dir(base_dir, self._user_id) / self.FILENAME
        # Back-compat: if a pre-multi-tenant file exists at the legacy
        # location, migrate it on first construction for the default user.
        legacy = base_dir / self.FILENAME
        if (
            self._user_id == DEFAULT_USER_ID
            and legacy.exists()
            and not self._path.exists()
        ):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_bytes(legacy.read_bytes())
            legacy.unlink()
        self._store = JsonStore(self._path)

    def load(self, default: PersistedPilotProfile) -> PersistedPilotProfile:
        data = self._store.read()
        if not data:
            return default
        profile = PilotProfile(
            pilot_id=data["pilot_id"],
            name=data["name"],
            position=Position(data["position"]),
            hourly_rate=Decimal(str(data["hourly_rate"])),
            fleet=data.get("fleet", "737"),
            sick_bank_days=int(data.get("sick_bank_days", 0)),
            pto_bank_days=int(data.get("pto_bank_days", 0)),
        )
        return PersistedPilotProfile(
            profile=profile,
            feed_url=data.get("feed_url", ""),
            feed_auto_update=bool(data.get("feed_auto_update", False)),
        )

    def save(self, persisted: PersistedPilotProfile) -> None:
        p = persisted.profile
        self._store.write(
            {
                "pilot_id": p.pilot_id,
                "name": p.name,
                "position": p.position.value,
                "hourly_rate": str(p.hourly_rate),
                "fleet": p.fleet,
                "sick_bank_days": p.sick_bank_days,
                "pto_bank_days": p.pto_bank_days,
                "feed_url": persisted.feed_url,
                "feed_auto_update": persisted.feed_auto_update,
            }
        )

    def reset(self) -> None:
        self._store.clear()
