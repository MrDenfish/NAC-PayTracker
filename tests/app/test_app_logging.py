"""_configure_app_logging — make nac_pay.* logs visible in process output."""

from __future__ import annotations

import logging

import pytest

from nac_pay.app.main import _configure_app_logging


@pytest.fixture
def _clean_nac_logger():
    """Snapshot + restore the process-global ``nac_pay`` logger so this
    test's mutations don't leak into the rest of the suite."""
    nac = logging.getLogger("nac_pay")
    saved = (list(nac.handlers), nac.level, nac.propagate)
    nac.handlers = []
    try:
        yield nac
    finally:
        nac.handlers, nac.level, nac.propagate = saved


def test_configure_adds_handler_at_info(_clean_nac_logger):
    _configure_app_logging()
    assert _clean_nac_logger.level == logging.INFO
    assert _clean_nac_logger.handlers
    assert _clean_nac_logger.propagate is False


def test_configure_is_idempotent(_clean_nac_logger):
    _configure_app_logging()
    count = len(_clean_nac_logger.handlers)
    _configure_app_logging()
    assert len(_clean_nac_logger.handlers) == count


def test_feed_updater_info_log_reaches_handler(_clean_nac_logger):
    """A child logger's INFO record reaches a handler on ``nac_pay`` — i.e.
    feed-updater activity will actually be emitted (not swallowed). Captured
    via our own handler since configure sets propagate=False (so caplog's
    root handler wouldn't see it)."""
    _configure_app_logging()
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    _clean_nac_logger.addHandler(_Capture())
    logging.getLogger("nac_pay.feed_updater").info("feed sweep: 1 checked")
    assert "feed sweep: 1 checked" in seen
