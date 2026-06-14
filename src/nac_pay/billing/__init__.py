"""Billing module — subscription state + Stripe integration.

Phase B1 (this commit): no-card 90-day trial, expiry check, gating
middleware, placeholder /billing page. Stripe is not yet wired.

Phase B2: Stripe Checkout for card collection + webhook handler.
Phase B3: Stripe Customer Portal for self-service.
"""

from .middleware import SubscriptionRequiredMiddleware
from .state import (
    ACTIVE_STATUSES,
    NUDGE_DAYS_BEFORE_END,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_NONE,
    STATUS_PAST_DUE,
    STATUS_TRIAL_EXPIRED,
    STATUS_TRIALING,
    TRIAL_LENGTH_DAYS,
    SubscriptionSnapshot,
    has_access,
    snapshot,
    start_trial,
)

__all__ = [
    "ACTIVE_STATUSES",
    "NUDGE_DAYS_BEFORE_END",
    "STATUS_ACTIVE",
    "STATUS_CANCELED",
    "STATUS_NONE",
    "STATUS_PAST_DUE",
    "STATUS_TRIAL_EXPIRED",
    "STATUS_TRIALING",
    "SubscriptionRequiredMiddleware",
    "SubscriptionSnapshot",
    "TRIAL_LENGTH_DAYS",
    "has_access",
    "snapshot",
    "start_trial",
]
