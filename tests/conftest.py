"""Global test fixtures.

Sets ``NAC_PAY_DATA_DIR`` to a fresh per-session temp directory so tests
never touch the user's real ``~/.nac-pay/data/`` and never see leftover
state from a previous run.

Phase 2 backend is SQL: per-test fixture drops + recreates every table
so each test starts with an empty database.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_storage_dir():
    with tempfile.TemporaryDirectory(prefix="nac-pay-test-") as tmp:
        os.environ["NAC_PAY_DATA_DIR"] = tmp
        # Explicitly clear any DATABASE_URL override that might be set in
        # the developer's shell — tests always use SQLite under the temp dir.
        os.environ.pop("NAC_PAY_DATABASE_URL", None)
        yield tmp


@pytest.fixture(autouse=True)
def _reset_persisted_state(_isolated_storage_dir):
    """Per-test reset: drop + recreate every table, clear pipeline cache.

    Keeps each test hermetic — a Settings POST in one test doesn't bleed
    into another test's load_day().
    """
    from nac_pay.app.services import invalidate_caches
    from nac_pay.storage import dispose_engine, reset_tables

    dispose_engine()
    reset_tables()
    invalidate_caches()
    yield
    invalidate_caches()
