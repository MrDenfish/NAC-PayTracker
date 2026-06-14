"""Billing module — subscription state + Stripe integration.

Phase B1: no-card 90-day trial, expiry check, gating middleware.
Phase B2 (this commit): Stripe Checkout for card collection + webhook
handler with idempotent state updates.
Phase B3: Stripe Customer Portal for self-service (cancel / update card).
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
from .stripe_adapter import (
    CheckoutSessionInputs,
    FakeStripeAdapter,
    RealStripeAdapter,
    StripeAdapter,
    get_stripe_adapter,
    reset_stripe_adapter,
)
from .webhooks import apply_stripe_event

__all__ = [
    "ACTIVE_STATUSES",
    "CheckoutSessionInputs",
    "FakeStripeAdapter",
    "NUDGE_DAYS_BEFORE_END",
    "RealStripeAdapter",
    "STATUS_ACTIVE",
    "STATUS_CANCELED",
    "STATUS_NONE",
    "STATUS_PAST_DUE",
    "STATUS_TRIAL_EXPIRED",
    "STATUS_TRIALING",
    "StripeAdapter",
    "SubscriptionRequiredMiddleware",
    "SubscriptionSnapshot",
    "TRIAL_LENGTH_DAYS",
    "apply_stripe_event",
    "get_stripe_adapter",
    "has_access",
    "reset_stripe_adapter",
    "snapshot",
    "start_trial",
]
