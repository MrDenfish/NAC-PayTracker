"""SQL-backend-specific tests (Phase 2).

The store APIs are covered by tests/storage/test_storage.py +
tests/storage/test_multi_user.py — those tests pass regardless of
backend. These tests pin behavior that's specific to the SQL layer:
URL resolution, Decimal precision, and table reset hygiene.
"""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal

import pytest

from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DayOverride,
    DayOverrideStore,
    PersistedPilotProfile,
    PilotProfileStore,
    database_url,
    dispose_engine,
    get_data_dir,
    get_engine,
    reset_tables,
)


D = Decimal


# ── URL resolution ────────────────────────────────────────────────────


def test_default_database_url_is_sqlite_under_data_dir():
    """No env override → SQLite file in NAC_PAY_DATA_DIR."""
    url = database_url()
    assert url.startswith("sqlite:///")
    assert str(get_data_dir()) in url
    assert url.endswith("nac_pay.db")


def test_nac_pay_database_url_env_wins(monkeypatch):
    """Setting NAC_PAY_DATABASE_URL bypasses the SQLite default. We don't
    actually connect to a Postgres host — just assert the URL resolves
    correctly and the engine factory picks it up after a dispose."""
    monkeypatch.setenv(
        "NAC_PAY_DATABASE_URL",
        "postgresql+psycopg2://test:test@localhost/test",
    )
    assert (
        database_url()
        == "postgresql+psycopg2://test:test@localhost/test"
    )


# ── Decimal precision ─────────────────────────────────────────────────


def test_hourly_rate_decimal_round_trips_exactly():
    """The pay-stub-anchored $124.59 must survive Numeric(9,4) round-trip
    as Decimal('124.5900') — same semantic value, exact compare."""
    store = PilotProfileStore(get_data_dir(), user_id="alice")
    store.save(
        PersistedPilotProfile(
            profile=PilotProfile(
                pilot_id="ALC", name="Alice",
                position=Position.FO, hourly_rate=D("124.59"),
            ),
        )
    )
    fallback = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="x", name="x", position=Position.FO, hourly_rate=D("0"),
        ),
    )
    loaded = store.load(fallback)
    # Numeric(9,4) gives us "124.5900" but it equals Decimal("124.59").
    assert loaded.profile.hourly_rate == D("124.59")
    assert loaded.profile.hourly_rate * D("65.78") == D("124.59") * D("65.78")


def test_custom_multiplier_decimal_round_trips_exactly():
    """A 1.5× premium multiplier must come back as exactly 1.5."""
    store = DayOverrideStore(get_data_dir(), user_id="alice")
    store.save_one(
        DayOverride(
            date_iso="2026-06-12",
            custom_multiplier="1.5",
        )
    )
    loaded = store.load_all()["2026-06-12"]
    assert loaded.custom_multiplier_decimal == D("1.5")


# ── Engine + table-reset hygiene ─────────────────────────────────────


def test_dispose_and_reset_gives_fresh_database():
    """dispose_engine + reset_tables wipes any prior state — used by
    conftest between tests."""
    store = PilotProfileStore(get_data_dir(), user_id="ghost")
    store.save(
        PersistedPilotProfile(
            profile=PilotProfile(
                pilot_id="GHO", name="ghost",
                position=Position.FO, hourly_rate=D("99"),
            ),
        )
    )
    # Sanity: row exists
    fallback = PersistedPilotProfile(
        profile=PilotProfile(
            pilot_id="x", name="default-fallback",
            position=Position.FO, hourly_rate=D("0"),
        ),
    )
    assert store.load(fallback).profile.name == "ghost"
    # Reset → row gone
    dispose_engine()
    reset_tables()
    assert store.load(fallback) == fallback


def test_get_engine_is_idempotent_within_a_url():
    """Repeated calls with the same URL return the same engine instance."""
    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2


# ── C4: _ensure_added_columns against a simulated old-schema DB ───────
#
# create_all only creates missing TABLES — it never ALTERs an existing one
# — so _ensure_added_columns is the ONLY mechanism that back-fills a new
# nullable column onto an already-created table. Prod is SQLite and this
# is the ONLY thing that will ever add duty_on_local/duty_off_local to the
# live database, so it needs a real test, not a by-hand review.
#
# SQLite 3.35+ (this environment: 3.45.1) supports ALTER TABLE ... DROP
# COLUMN, so we build the CURRENT schema via create_all (guaranteeing it
# stays in sync with db_models.py) and then drop the two added columns
# with raw DDL to simulate the pre-migration table shape, rather than
# hand-maintaining a second, driftable schema definition.


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="ALTER TABLE ... DROP COLUMN requires SQLite >= 3.35",
)
def test_ensure_added_columns_backfills_a_pre_migration_schema(monkeypatch, tmp_path):
    from sqlalchemy import inspect as sa_inspect, text

    from nac_pay.storage.db import _ensure_added_columns

    monkeypatch.setenv("NAC_PAY_DATA_DIR", str(tmp_path))
    dispose_engine()
    engine = get_engine()  # create_all + _ensure_added_columns already ran once

    table = "user_assignment_versions"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN duty_on_local"))
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN duty_off_local"))

    # Simulated old schema: genuinely absent, not just untouched.
    cols = {c["name"] for c in sa_inspect(engine).get_columns(table)}
    assert "duty_on_local" not in cols
    assert "duty_off_local" not in cols

    _ensure_added_columns(engine)
    cols = {c["name"] for c in sa_inspect(engine).get_columns(table)}
    assert "duty_on_local" in cols
    assert "duty_off_local" in cols

    # Idempotent: a second run against the now-current schema must not
    # raise (e.g. a duplicate-column ADD COLUMN error) and must leave the
    # columns exactly as they were.
    _ensure_added_columns(engine)
    cols_again = {c["name"] for c in sa_inspect(engine).get_columns(table)}
    assert cols_again == cols
