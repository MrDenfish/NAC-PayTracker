"""Per-date pilot overrides — what the pilot changed via the GUI.

Stored as ``{date_iso: {field: value}}`` in ``day_overrides.json``. Fields
with value ``None`` are omitted so an override that resets back to
default removes itself from the file.

The schema deliberately accepts strings for enum values so the JSON file
remains human-readable. Validation happens at load_all() time when the
strings are coerced to enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class DayOverride:
    date_iso: str
    reason_code: str | None = None         # ReasonCode enum value
    premium_category: str | None = None    # PremiumCategory enum value
    custom_multiplier: str | None = None   # Decimal as string for round-trip
    entry_mode: str | None = None          # EntryMode enum value

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
    FILENAME = "day_overrides.json"

    def __init__(self, base_dir: Path, user_id: str | None = None):
        from . import JsonStore
        from .users import DEFAULT_USER_ID, user_dir
        self._user_id = user_id or DEFAULT_USER_ID
        self._path = user_dir(base_dir, self._user_id) / self.FILENAME
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

    def load_all(self) -> dict[str, DayOverride]:
        raw = self._store.read()
        out: dict[str, DayOverride] = {}
        for date_iso, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            out[date_iso] = DayOverride(
                date_iso=date_iso,
                reason_code=fields.get("reason_code"),
                premium_category=fields.get("premium_category"),
                custom_multiplier=fields.get("custom_multiplier"),
                entry_mode=fields.get("entry_mode"),
            )
        return out

    def save_one(self, override: DayOverride) -> None:
        raw = self._store.read()
        if override.is_empty:
            raw.pop(override.date_iso, None)
        else:
            raw[override.date_iso] = {
                k: v for k, v in {
                    "reason_code": override.reason_code,
                    "premium_category": override.premium_category,
                    "custom_multiplier": override.custom_multiplier,
                    "entry_mode": override.entry_mode,
                }.items()
                if v is not None and v != ""
            }
        self._store.write(raw)

    def delete_one(self, date_iso: str) -> None:
        raw = self._store.read()
        if date_iso in raw:
            raw.pop(date_iso)
            self._store.write(raw)

    def reset(self) -> None:
        self._store.clear()
