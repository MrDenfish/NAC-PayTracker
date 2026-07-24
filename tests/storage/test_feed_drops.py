"""FeedDropDecisionStore: reject/confirm decisions on feed-detected drops."""

from __future__ import annotations

import pytest

from nac_pay.storage import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    FeedDropDecisionStore,
)


def _store(uid: str = "feed-drop-tests") -> FeedDropDecisionStore:
    return FeedDropDecisionStore(user_id=uid)


def test_absent_decision_is_none():
    assert _store().get("2026-07-31") is None


def test_reject_then_read_back():
    s = _store()
    s.set("2026-07-31", STATUS_REJECTED)
    assert s.get("2026-07-31") == STATUS_REJECTED
    assert s.rejected_dates_for_month(2026, 7) == {"2026-07-31"}


def test_month_scoping():
    s = _store("feed-drop-scope")
    s.set("2026-07-31", STATUS_REJECTED)
    s.set("2026-08-02", STATUS_REJECTED)
    assert s.rejected_dates_for_month(2026, 7) == {"2026-07-31"}
    assert s.rejected_dates_for_month(2026, 8) == {"2026-08-02"}


def test_clear_reverts_to_proposed():
    s = _store("feed-drop-clear")
    s.set("2026-07-31", STATUS_REJECTED)
    s.clear("2026-07-31")
    assert s.get("2026-07-31") is None
    assert s.rejected_dates_for_month(2026, 7) == set()


def test_confirmed_rows_not_in_rejected_dates():
    s = _store("feed-drop-confirmed")
    s.set("2026-07-31", STATUS_CONFIRMED)
    assert s.get("2026-07-31") == STATUS_CONFIRMED
    assert s.rejected_dates_for_month(2026, 7) == set()


def test_invalid_status_raises():
    with pytest.raises(ValueError):
        _store().set("2026-07-31", "MAYBE")


def test_users_are_isolated():
    a = _store("feed-drop-user-a")
    b = _store("feed-drop-user-b")
    a.set("2026-07-31", STATUS_REJECTED)
    assert b.get("2026-07-31") is None
