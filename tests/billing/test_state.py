"""Subscription state unit tests — trial start, expiry, status transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nac_pay.auth import create_user, mark_email_verified
from nac_pay.billing import (
    ACTIVE_STATUSES,
    NUDGE_DAYS_BEFORE_END,
    STATUS_ACTIVE,
    STATUS_NONE,
    STATUS_TRIAL_EXPIRED,
    STATUS_TRIALING,
    TRIAL_LENGTH_DAYS,
    has_access,
    snapshot,
    start_trial,
)
from nac_pay.storage import default_user
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _set_trial_end(user_id: str, when: datetime) -> None:
    """Test helper — slide a user's trial-end timestamp without waiting."""
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        assert row is not None
        row.trial_ends_at = when.isoformat(timespec="seconds")


# ── start_trial ──────────────────────────────────────────────────────


def test_start_trial_sets_trialing_with_90_day_window():
    uid = create_user("alice@example.com", "long enough password")
    start_trial(uid)
    snap = snapshot(uid)
    assert snap.status == STATUS_TRIALING
    assert snap.trial_ends_at is not None
    # 90 days from now (allow 5 minutes of slack for test execution time).
    expected = datetime.now(timezone.utc) + timedelta(days=TRIAL_LENGTH_DAYS)
    assert abs((snap.trial_ends_at - expected).total_seconds()) < 300


def test_start_trial_is_idempotent():
    """Calling start_trial twice doesn't extend the trial."""
    uid = create_user("bob@example.com", "long enough password")
    start_trial(uid)
    first_end = snapshot(uid).trial_ends_at
    start_trial(uid)
    assert snapshot(uid).trial_ends_at == first_end


def test_start_trial_doesnt_downgrade_active_subscription():
    uid = create_user("carol@example.com", "long enough password")
    # Promote her to ACTIVE manually (simulating a Stripe webhook landing).
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.subscription_status = STATUS_ACTIVE
    # start_trial should NOT downgrade her back to TRIALING.
    start_trial(uid)
    assert snapshot(uid).status == STATUS_ACTIVE


# ── Expiry computation ─────────────────────────────────────────────


def test_trialing_expires_when_now_passes_trial_ends_at():
    uid = create_user("dave@example.com", "long enough password")
    start_trial(uid)
    # Move the trial-end into the past.
    _set_trial_end(uid, datetime.now(timezone.utc) - timedelta(hours=1))
    snap = snapshot(uid)
    assert snap.status == STATUS_TRIAL_EXPIRED
    # Persisted is still TRIALING — the expired view is computed.
    assert snap.persisted_status == STATUS_TRIALING


def test_days_left_counts_down_correctly():
    uid = create_user("eve@example.com", "long enough password")
    start_trial(uid)
    snap = snapshot(uid)
    # Fresh trial = ~90 days left
    assert 89 <= snap.days_left_in_trial <= 90

    # Slide to 5 days remaining
    _set_trial_end(uid, datetime.now(timezone.utc) + timedelta(days=5))
    assert snapshot(uid).days_left_in_trial in (5, 6)

    # Slide to a few hours remaining → at least 1 day shown
    _set_trial_end(uid, datetime.now(timezone.utc) + timedelta(hours=4))
    assert snapshot(uid).days_left_in_trial == 1


def test_nudge_active_inside_last_ten_days():
    uid = create_user("frank@example.com", "long enough password")
    start_trial(uid)
    # Outside the nudge window
    assert snapshot(uid).nudge_active is False

    # Slide to 9 days remaining → inside window
    _set_trial_end(uid, datetime.now(timezone.utc) + timedelta(days=9))
    assert snapshot(uid).nudge_active is True
    assert snapshot(uid).days_left_in_trial <= NUDGE_DAYS_BEFORE_END


# ── Access gate ─────────────────────────────────────────────────────


def test_default_user_always_has_access():
    snap = snapshot(default_user().user_id)
    assert snap.is_default_user is True
    assert snap.status == STATUS_ACTIVE
    assert has_access(snap) is True


def test_user_with_no_trial_has_no_access():
    uid = create_user("greg@example.com", "long enough password")
    snap = snapshot(uid)
    assert snap.status == STATUS_NONE
    assert has_access(snap) is False


def test_trialing_has_access():
    uid = create_user("hank@example.com", "long enough password")
    start_trial(uid)
    assert has_access(snapshot(uid)) is True


def test_trial_expired_loses_access():
    uid = create_user("ivy@example.com", "long enough password")
    start_trial(uid)
    _set_trial_end(uid, datetime.now(timezone.utc) - timedelta(hours=1))
    snap = snapshot(uid)
    assert snap.status == STATUS_TRIAL_EXPIRED
    assert has_access(snap) is False
    assert STATUS_TRIAL_EXPIRED not in ACTIVE_STATUSES
