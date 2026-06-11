"""Global test fixtures.

Sets ``NAC_PAY_DATA_DIR`` to a fresh per-session temp directory so tests
never touch the user's real ``~/.nac-pay/data/`` and never see leftover
state from a previous run.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_storage_dir():
    with tempfile.TemporaryDirectory(prefix="nac-pay-test-") as tmp:
        os.environ["NAC_PAY_DATA_DIR"] = tmp
        yield tmp


@pytest.fixture(autouse=True)
def _reset_persisted_state(_isolated_storage_dir):
    """Per-test reset: wipe stored profile + overrides + pipeline cache.

    Keeps each test hermetic — a Settings POST in one test doesn't bleed
    into another test's load_day().
    """
    from nac_pay.app.services import (
        invalidate_caches,
        override_store,
        profile_store,
    )
    profile_store().reset()
    override_store().reset()
    invalidate_caches()
    yield
    profile_store().reset()
    override_store().reset()
    invalidate_caches()
