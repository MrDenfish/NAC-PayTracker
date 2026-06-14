"""Subscription state — trial activation, status transitions, expiry computation.

Phase B1 focuses on the no-card trial path:

- On email verification → ``start_trial(user_id)`` sets status ``TRIALING``
  and ``trial_ends_at = now + 90 days``.
- ``effective_status(user_id)`` returns the persisted status with one
  computed override: ``TRIALING`` past its expiry returns ``TRIAL_EXPIRED``
  without writing the row (the next webhook will materialize it; for now
  the computed view is the source of truth for access decisions).

Phase B2 will add the Stripe wiring that promotes ``TRIALING`` /
``TRIAL_EXPIRED`` → ``ACTIVE`` via Checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy import select

from nac_pay.storage import default_user
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

TRIAL_LENGTH_DAYS: Final = 90
NUDGE_DAYS_BEFORE_END: Final = 10   # banner appears with 10 days left


# Status constants — kept as bare strings to match the column type.
STATUS_NONE = "NONE"
STATUS_TRIALING = "TRIALING"
STATUS_TRIAL_EXPIRED = "TRIAL_EXPIRED"
STATUS_ACTIVE = "ACTIVE"
STATUS_PAST_DUE = "PAST_DUE"
STATUS_CANCELED = "CANCELED"

ACTIVE_STATUSES: frozenset[str] = frozenset(
    {STATUS_TRIALING, STATUS_ACTIVE, STATUS_PAST_DUE}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    """Tolerate both '+00:00' and 'Z' suffixes."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(frozen=True)
class SubscriptionSnapshot:
    user_id: str
    status: str                # the effective status (after expiry check)
    persisted_status: str      # what's actually in the row
    trial_ends_at: datetime | None
    days_left_in_trial: int    # 0 if not trialing or expired
    nudge_active: bool         # show "add payment" banner
    is_default_user: bool      # the bundled dev user — never gated


def start_trial(user_id: str) -> None:
    """Mark the user as TRIALING starting now. Idempotent for callers
    that hit it twice — we never extend an existing trial here."""
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            return
        if row.subscription_status in ACTIVE_STATUSES:
            return   # don't downgrade an ACTIVE/PAST_DUE customer back to TRIALING
        if row.subscription_status == STATUS_TRIALING and row.trial_ends_at:
            return   # leave existing trial alone
        row.subscription_status = STATUS_TRIALING
        row.trial_ends_at = _iso(_utcnow() + timedelta(days=TRIAL_LENGTH_DAYS))


def snapshot(user_id: str) -> SubscriptionSnapshot:
    """Read-only view used by middleware + dashboard banner."""
    # The bundled default dev user is never gated.
    is_default = user_id == default_user().user_id

    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()

    if row is None:
        return SubscriptionSnapshot(
            user_id=user_id,
            status=STATUS_ACTIVE if is_default else STATUS_NONE,
            persisted_status=STATUS_NONE,
            trial_ends_at=None,
            days_left_in_trial=0,
            nudge_active=False,
            is_default_user=is_default,
        )

    persisted = row.subscription_status
    trial_end_dt = _parse_iso(row.trial_ends_at) if row.trial_ends_at else None

    effective = persisted
    days_left = 0
    nudge = False

    if persisted == STATUS_TRIALING and trial_end_dt is not None:
        remaining = (trial_end_dt - _utcnow()).total_seconds()
        if remaining <= 0:
            effective = STATUS_TRIAL_EXPIRED
        else:
            # round up so 14h left displays as "1 day"
            days_left = max(1, int(remaining // 86400) + (1 if remaining % 86400 else 0))
            nudge = days_left <= NUDGE_DAYS_BEFORE_END

    # Default user is never gated regardless of persisted state.
    if is_default:
        effective = STATUS_ACTIVE

    return SubscriptionSnapshot(
        user_id=user_id,
        status=effective,
        persisted_status=persisted,
        trial_ends_at=trial_end_dt,
        days_left_in_trial=days_left,
        nudge_active=nudge,
        is_default_user=is_default,
    )


def has_access(snap: SubscriptionSnapshot) -> bool:
    """Truthy when the user can reach the gated app surface."""
    if snap.is_default_user:
        return True
    return snap.status in ACTIVE_STATUSES
