"""FeedReassignmentDecisionStore: confirm/reject + optional PCH override."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nac_pay.storage import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    FeedReassignmentDecisionStore,
)


def _store(uid: str = "reassign-tests") -> FeedReassignmentDecisionStore:
    return FeedReassignmentDecisionStore(user_id=uid)


def test_absent_decision_is_none():
    assert _store().get("2026-07-06", "732/732/733") is None


def test_confirm_then_read_back():
    s = _store()
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED)
    assert s.get("2026-07-06", "732/732/733") == STATUS_CONFIRMED
    assert ("2026-07-06", "732/732/733") in s.decisions_for_month(2026, 7)


def test_confirm_with_pch_override_round_trips():
    s = _store("reassign-pch")
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED, pch=Decimal("5.17"))
    overrides = s.pch_overrides_for_month(2026, 7)
    assert overrides[("2026-07-06", "732/732/733")] == Decimal("5.17")


def test_reject_clears_any_pch_override():
    s = _store("reassign-reject-clears")
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED, pch=Decimal("5.17"))
    s.set("2026-07-06", "732/732/733", STATUS_REJECTED)
    assert s.get("2026-07-06", "732/732/733") == STATUS_REJECTED
    assert ("2026-07-06", "732/732/733") not in s.pch_overrides_for_month(2026, 7)


def test_reconfirm_without_pch_clears_override():
    s = _store("reassign-reconfirm")
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED, pch=Decimal("5.17"))
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED)  # no pch this time
    assert ("2026-07-06", "732/732/733") not in s.pch_overrides_for_month(2026, 7)


def test_invalid_pch_rejected():
    s = _store("reassign-bad-pch")
    with pytest.raises(ValueError):
        s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED, pch=Decimal("0"))


def test_month_scoping_of_overrides():
    s = _store("reassign-scope")
    s.set("2026-07-06", "732/732/733", STATUS_CONFIRMED, pch=Decimal("5.17"))
    s.set("2026-08-06", "800/801", STATUS_CONFIRMED, pch=Decimal("4.00"))
    jul = s.pch_overrides_for_month(2026, 7)
    assert set(jul) == {("2026-07-06", "732/732/733")}
