"""Apply Stripe webhook events to local subscription state.

We treat Stripe as the source of truth and our DB columns as a cache.
The handler is idempotent — replaying the same event must produce the
same state — so duplicate webhook deliveries don't break anything.

Events we react to (Stripe sends many more; we ignore the rest):

- ``checkout.session.completed``: card collected, subscription created.
- ``customer.subscription.updated``: any status / period change.
- ``customer.subscription.deleted``: subscription canceled.
- ``invoice.payment_succeeded``: renewal payment landed.
- ``invoice.payment_failed``: renewal failed; Stripe enters grace.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

from .state import (
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
)


def _from_unix(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _find_user_row_by_customer_id(sess, customer_id: str | None) -> UserRow | None:
    if not customer_id:
        return None
    return sess.execute(
        select(UserRow).where(UserRow.stripe_customer_id == customer_id)
    ).scalar_one_or_none()


def apply_stripe_event(event: dict) -> bool:
    """Apply an event to local state. Returns True if a row was changed,
    False if the event was ignored (unrecognized type, no matching user,
    etc.). Never raises on unrecognized payloads — Stripe's API evolves
    and we shouldn't 500 on a new event shape."""
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}
    customer_id = obj.get("customer")
    subscription_id = obj.get("subscription") or obj.get("id")

    if event_type == "checkout.session.completed":
        return _apply_checkout_completed(customer_id, subscription_id, obj)
    if event_type == "customer.subscription.updated":
        return _apply_subscription_updated(customer_id, subscription_id, obj)
    if event_type == "customer.subscription.deleted":
        return _apply_subscription_deleted(customer_id, subscription_id, obj)
    if event_type == "invoice.payment_succeeded":
        return _apply_payment_succeeded(customer_id, subscription_id, obj)
    if event_type == "invoice.payment_failed":
        return _apply_payment_failed(customer_id, subscription_id, obj)
    return False


def _apply_checkout_completed(
    customer_id: str | None, subscription_id: str | None, obj: dict
) -> bool:
    if not customer_id:
        return False
    with session_scope() as sess:
        row = _find_user_row_by_customer_id(sess, customer_id)
        if row is None:
            return False
        row.subscription_status = STATUS_ACTIVE
        if subscription_id:
            row.stripe_subscription_id = subscription_id
        period_end = _from_unix(obj.get("current_period_end"))
        if period_end:
            row.current_period_end = period_end
        return True


def _apply_subscription_updated(
    customer_id: str | None, subscription_id: str | None, obj: dict
) -> bool:
    if not customer_id:
        return False
    stripe_status = obj.get("status", "")
    with session_scope() as sess:
        row = _find_user_row_by_customer_id(sess, customer_id)
        if row is None:
            return False
        # Map Stripe statuses → ours. We only set what we can confidently
        # mirror; everything else stays as-is (no spurious downgrades).
        if stripe_status in ("active", "trialing"):
            row.subscription_status = STATUS_ACTIVE
        elif stripe_status == "past_due":
            row.subscription_status = STATUS_PAST_DUE
        elif stripe_status in ("canceled", "incomplete_expired", "unpaid"):
            row.subscription_status = STATUS_CANCELED
        if subscription_id:
            row.stripe_subscription_id = subscription_id
        period_end = _from_unix(obj.get("current_period_end"))
        if period_end:
            row.current_period_end = period_end
        return True


def _apply_subscription_deleted(
    customer_id: str | None, subscription_id: str | None, obj: dict
) -> bool:
    if not customer_id:
        return False
    with session_scope() as sess:
        row = _find_user_row_by_customer_id(sess, customer_id)
        if row is None:
            return False
        row.subscription_status = STATUS_CANCELED
        return True


def _apply_payment_succeeded(
    customer_id: str | None, subscription_id: str | None, obj: dict
) -> bool:
    if not customer_id:
        return False
    with session_scope() as sess:
        row = _find_user_row_by_customer_id(sess, customer_id)
        if row is None:
            return False
        # A successful payment after past-due means we're active again.
        if row.subscription_status == STATUS_PAST_DUE:
            row.subscription_status = STATUS_ACTIVE
        period_end = _from_unix(
            obj.get("period_end") or obj.get("current_period_end")
        )
        if period_end:
            row.current_period_end = period_end
        return True


def _apply_payment_failed(
    customer_id: str | None, subscription_id: str | None, obj: dict
) -> bool:
    if not customer_id:
        return False
    with session_scope() as sess:
        row = _find_user_row_by_customer_id(sess, customer_id)
        if row is None:
            return False
        row.subscription_status = STATUS_PAST_DUE
        return True
