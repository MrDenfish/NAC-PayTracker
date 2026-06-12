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
    """Per-test reset: wipe ALL persisted state (every user) + pipeline cache.

    Keeps each test hermetic — a Settings POST in one test doesn't bleed
    into another test's load_day(), and multi-user tests don't leave
    synthetic users lying around for the bundled DFI pipeline to choke on.
    """
    import shutil
    from pathlib import Path

    from nac_pay.app.services import invalidate_caches

    def _wipe():
        data_dir = Path(_isolated_storage_dir)
        if data_dir.exists():
            for entry in data_dir.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        pass
        invalidate_caches()

    _wipe()
    yield
    _wipe()
