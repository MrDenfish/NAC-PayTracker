"""Storage layer tests — JsonStore + PilotProfileStore + DayOverrideStore."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DayOverride,
    DayOverrideStore,
    JsonStore,
    PersistedPilotProfile,
    PilotProfileStore,
    get_data_dir,
)


D = Decimal


# ── JsonStore ──────────────────────────────────────────────────────────


def test_jsonstore_round_trip(tmp_path):
    store = JsonStore(tmp_path / "x.json")
    assert store.read() == {}
    store.write({"hello": "world", "n": 42})
    assert store.read() == {"hello": "world", "n": 42}


def test_jsonstore_atomic_write_uses_tmp_then_rename(tmp_path):
    """The temp file shouldn't persist after a successful write."""
    store = JsonStore(tmp_path / "a.json")
    store.write({"k": "v"})
    leftover = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_jsonstore_clear_removes_file(tmp_path):
    store = JsonStore(tmp_path / "y.json")
    store.write({"k": "v"})
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    # Clearing an absent file is a no-op.
    store.clear()


# ── PilotProfileStore ─────────────────────────────────────────────────


def _default_persisted() -> PersistedPilotProfile:
    return PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI",
            name="Dennis FISHER",
            position=Position.FO,
            hourly_rate=D("124.59"),
        ),
    )


def test_profile_store_returns_default_when_file_absent():
    store = PilotProfileStore(get_data_dir())
    default = _default_persisted()
    loaded = store.load(default)
    assert loaded == default


def test_profile_store_save_then_reload_preserves_decimal_and_banks():
    """All field types must round-trip: Decimal hourly_rate, int banks,
    enum Position, bool feed_auto_update."""
    store = PilotProfileStore(get_data_dir())
    persisted = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI",
            name="Dennis FISHER",
            position=Position.CPT,
            hourly_rate=D("130.42"),
            sick_bank_days=8,
            pto_bank_days=15,
        ),
        feed_url="https://example.com/feed.ics",
        feed_auto_update=True,
    )
    store.save(persisted)
    reloaded = store.load(_default_persisted())
    assert reloaded == persisted
    assert reloaded.profile.hourly_rate == D("130.42")
    assert reloaded.profile.position is Position.CPT


def test_profile_store_reset_clears_back_to_default():
    store = PilotProfileStore(get_data_dir())
    persisted = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="DFI", name="X",
            position=Position.FO, hourly_rate=D("99.99"),
        ),
    )
    store.save(persisted)
    store.reset()
    assert store.load(_default_persisted()) == _default_persisted()


# ── DayOverrideStore ─────────────────────────────────────────────────


def test_override_store_empty_when_no_file():
    assert DayOverrideStore(get_data_dir()).load_all() == {}


def test_override_store_save_and_reload():
    store = DayOverrideStore(get_data_dir())
    store.save_one(DayOverride(date_iso="2026-06-12", reason_code="SICK"))
    store.save_one(
        DayOverride(
            date_iso="2026-06-17",
            premium_category="OPEN_TIME_MID_MONTH",
            custom_multiplier="1.5",
        )
    )
    loaded = store.load_all()
    assert loaded == {
        "2026-06-12": DayOverride(date_iso="2026-06-12", reason_code="SICK"),
        "2026-06-17": DayOverride(
            date_iso="2026-06-17",
            premium_category="OPEN_TIME_MID_MONTH",
            custom_multiplier="1.5",
        ),
    }


def test_override_store_empty_override_removes_record():
    """Saving a DayOverride with all fields None should delete its entry."""
    store = DayOverrideStore(get_data_dir())
    store.save_one(DayOverride(date_iso="2026-06-12", reason_code="SICK"))
    assert "2026-06-12" in store.load_all()
    store.save_one(DayOverride(date_iso="2026-06-12"))
    assert "2026-06-12" not in store.load_all()


def test_override_store_delete_one():
    store = DayOverrideStore(get_data_dir())
    store.save_one(DayOverride(date_iso="2026-06-12", reason_code="SICK"))
    store.save_one(DayOverride(date_iso="2026-06-13", reason_code="PTO"))
    store.delete_one("2026-06-12")
    remaining = store.load_all()
    assert "2026-06-12" not in remaining
    assert "2026-06-13" in remaining
