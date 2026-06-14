"""Stripe API adapter — abstracts the SDK behind a small protocol so
tests can drive the checkout / webhook flow without network calls.

Production uses ``RealStripeAdapter`` which delegates to the ``stripe``
package directly. Tests use ``FakeStripeAdapter`` which captures intended
calls and returns predictable IDs, and exposes a ``construct_event_dict``
helper so test code can simulate webhook posts.

Selection in services: ``get_stripe_adapter()`` reads ``STRIPE_BACKEND``
(``real`` / ``fake``) — defaults to ``fake`` so missing prod config can't
silently let tests hit Stripe.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CheckoutSessionInputs:
    """Captured args from a checkout session creation request (tests)."""

    customer_id: str
    price_id: str
    success_url: str
    cancel_url: str


class StripeAdapter(Protocol):
    def create_or_get_customer(
        self, email: str, existing_customer_id: str | None = None
    ) -> str: ...
    def create_checkout_session(
        self,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str: ...
    def create_portal_session(
        self, customer_id: str, return_url: str
    ) -> str: ...
    def parse_webhook(self, payload: bytes, signature: str) -> dict: ...


# ── Real (production) ────────────────────────────────────────────────


class RealStripeAdapter:
    def __init__(self, api_key: str, webhook_secret: str):
        import stripe
        self._stripe = stripe
        self._stripe.api_key = api_key
        self._webhook_secret = webhook_secret

    def create_or_get_customer(
        self, email: str, existing_customer_id: str | None = None
    ) -> str:
        if existing_customer_id:
            return existing_customer_id
        cust = self._stripe.Customer.create(email=email)
        return cust.id

    def create_checkout_session(
        self,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        session = self._stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
        )
        return session.url

    def create_portal_session(self, customer_id: str, return_url: str) -> str:
        session = self._stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url,
        )
        return session.url

    def parse_webhook(self, payload: bytes, signature: str) -> dict:
        """Verify the Stripe signature and return the parsed event dict.
        Raises if signature invalid (caller turns this into 400)."""
        event = self._stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature,
            secret=self._webhook_secret,
        )
        return dict(event)


# ── Fake (tests) ─────────────────────────────────────────────────────


@dataclass
class _FakeState:
    customers: dict[str, str] = field(default_factory=dict)  # customer_id → email
    next_customer_seq: int = 1
    next_session_seq: int = 1
    last_checkout_inputs: CheckoutSessionInputs | None = None
    last_portal_return_url: str | None = None


class FakeStripeAdapter:
    """In-memory fake. Returns predictable IDs (``cus_test_1``,
    ``test_checkout_1``, etc.) and skips signature verification — tests
    that need to ensure a signature-failure path use ``raise_on_parse``.
    """

    BAD_SIGNATURE = "BAD"

    def __init__(self) -> None:
        self.state = _FakeState()
        self._raise_on_parse: bool = False

    # API surface ----------------------------------------------------

    def create_or_get_customer(
        self, email: str, existing_customer_id: str | None = None
    ) -> str:
        if existing_customer_id:
            return existing_customer_id
        cust_id = f"cus_test_{self.state.next_customer_seq}"
        self.state.next_customer_seq += 1
        self.state.customers[cust_id] = email
        return cust_id

    def create_checkout_session(
        self,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        inputs = CheckoutSessionInputs(
            customer_id=customer_id,
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        self.state.last_checkout_inputs = inputs
        seq = self.state.next_session_seq
        self.state.next_session_seq += 1
        return f"https://fake-stripe.test/checkout/{seq}"

    def create_portal_session(self, customer_id: str, return_url: str) -> str:
        self.state.last_portal_return_url = return_url
        return f"https://fake-stripe.test/portal/{customer_id}"

    def parse_webhook(self, payload: bytes, signature: str) -> dict:
        if signature == self.BAD_SIGNATURE:
            raise ValueError("bad signature")
        return json.loads(payload.decode("utf-8"))

    # Test helpers ---------------------------------------------------

    def build_event(
        self,
        event_type: str,
        *,
        customer_id: str = "cus_test_1",
        subscription_id: str = "sub_test_1",
        current_period_end: int | None = None,
    ) -> dict:
        """Build a synthetic event dict shaped like Stripe's payloads,
        for use in webhook tests."""
        data_object: dict = {
            "customer": customer_id,
            "subscription": subscription_id,
        }
        if current_period_end is not None:
            data_object["current_period_end"] = current_period_end
        return {
            "type": event_type,
            "data": {"object": data_object},
        }


# ── Selector ────────────────────────────────────────────────────────


_ADAPTER: StripeAdapter | None = None


def get_stripe_adapter() -> StripeAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        backend = os.environ.get("STRIPE_BACKEND", "fake").lower()
        if backend == "real":
            api_key = os.environ.get("STRIPE_API_KEY")
            webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
            if not api_key or not webhook_secret:
                raise RuntimeError(
                    "STRIPE_BACKEND=real requires STRIPE_API_KEY and "
                    "STRIPE_WEBHOOK_SECRET env vars"
                )
            _ADAPTER = RealStripeAdapter(api_key, webhook_secret)
        elif backend == "fake":
            _ADAPTER = FakeStripeAdapter()
        else:
            raise ValueError(f"Unknown STRIPE_BACKEND={backend!r}")
    return _ADAPTER


def reset_stripe_adapter() -> None:
    """Test hook — drop the cached adapter so the next call rebuilds it."""
    global _ADAPTER
    _ADAPTER = None
