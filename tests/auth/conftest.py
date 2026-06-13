"""Per-test setup for auth tests: reset the in-process email sender so
each test starts with an empty captured-message list."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_email_sender():
    from nac_pay.auth.emails import reset_email_sender
    reset_email_sender()
    yield
    reset_email_sender()
