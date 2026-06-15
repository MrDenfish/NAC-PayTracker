"""Stripe Checkout + webhook event tests.

The FakeStripeAdapter captures every Checkout Session arg the route
would pass to Stripe, and lets us POST synthetic webhook bodies to
``/webhooks/stripe`` to exercise the state transitions Stripe would
trigger in production.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import get_email_sender
from nac_pay.billing import (
    FakeStripeAdapter,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
    STATUS_TRIALING,
    get_stripe_adapter,
    reset_stripe_adapter,
)
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    """Sign up, verify email, mark onboarding done (so we test the
    subscription gate, not the wizard redirect)."""
    from nac_pay.onboarding import mark_completed
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    with session_scope() as sess:
        uid = sess.execute(
            select(UserRow.user_id).where(UserRow.email == email.lower())
        ).scalar_one()
    mark_completed(uid)
    return uid


def _set_customer_id(user_id: str, customer_id: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one()
        row.stripe_customer_id = customer_id


def _read_status(user_id: str) -> tuple[str, str | None]:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow.subscription_status, UserRow.stripe_subscription_id)
            .where(UserRow.user_id == user_id)
        ).first()
        return (row.subscription_status, row.stripe_subscription_id)


@pytest.fixture(autouse=True)
def _isolated_stripe(monkeypatch):
    """Each test gets a fresh FakeStripeAdapter so captured args don't
    leak across cases."""
    reset_stripe_adapter()
    monkeypatch.setenv("STRIPE_BACKEND", "fake")
    monkeypatch.setenv("BASE_URL", "https://app.test")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_test_99")
    yield
    reset_stripe_adapter()


# ── FakeStripeAdapter directly ────────────────────────────────────────


def test_fake_adapter_returns_predictable_ids_and_captures_args():
    adapter = FakeStripeAdapter()
    cust = adapter.create_or_get_customer("a@b.com")
    assert cust == "cus_test_1"
    url = adapter.create_checkout_session(
        customer_id=cust, price_id="price_x",
        success_url="https://app.test/billing?success=1",
        cancel_url="https://app.test/billing",
    )
    assert url == "https://fake-stripe.test/checkout/1"
    captured = adapter.state.last_checkout_inputs
    assert captured is not None
    assert captured.customer_id == "cus_test_1"
    assert captured.price_id == "price_x"
    assert captured.success_url.endswith("/billing?success=1")


def test_fake_adapter_reuses_existing_customer_id():
    adapter = FakeStripeAdapter()
    cust = adapter.create_or_get_customer("a@b.com", "cus_existing")
    assert cust == "cus_existing"


def test_fake_adapter_raises_on_bad_signature():
    adapter = FakeStripeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_webhook(b"{}", FakeStripeAdapter.BAD_SIGNATURE)


# ── /billing/upgrade end-to-end via Fake ─────────────────────────────


def test_billing_upgrade_creates_customer_and_redirects_to_checkout(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "alice@example.com")

    r = isolated.post("/billing/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://fake-stripe.test/checkout/")

    # The user row now carries the captured Stripe customer_id.
    with session_scope() as sess:
        cust_id = sess.execute(
            select(UserRow.stripe_customer_id).where(UserRow.user_id == uid)
        ).scalar_one()
        assert cust_id is not None
        assert cust_id.startswith("cus_test_")

    adapter: FakeStripeAdapter = get_stripe_adapter()  # type: ignore[assignment]
    inputs = adapter.state.last_checkout_inputs
    assert inputs is not None
    assert inputs.price_id == "price_test_99"
    assert inputs.success_url == "https://app.test/billing?success=1"
    assert inputs.cancel_url == "https://app.test/billing"


def test_repeated_upgrade_reuses_same_customer(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "bob@example.com")
    isolated.post("/billing/upgrade", follow_redirects=False)
    isolated.post("/billing/upgrade", follow_redirects=False)
    # Only one customer was created — the second call reused the stored id.
    adapter: FakeStripeAdapter = get_stripe_adapter()  # type: ignore[assignment]
    assert adapter.state.next_customer_seq == 2   # only one create


# ── Webhook event dispatch ──────────────────────────────────────────


def _post_webhook(client: TestClient, event: dict, *, signature: str = "ok") -> dict:
    r = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )
    return {"status": r.status_code, "body": r.text}


def test_webhook_checkout_completed_promotes_to_active(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "carol@example.com")
    _set_customer_id(uid, "cus_test_42")

    assert _read_status(uid)[0] == STATUS_TRIALING

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer": "cus_test_42",
            "subscription": "sub_test_42",
            "current_period_end": int(
                datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
            ),
        }},
    }
    result = _post_webhook(isolated, event)
    assert result["status"] == 200

    status, sub_id = _read_status(uid)
    assert status == STATUS_ACTIVE
    assert sub_id == "sub_test_42"


def test_webhook_subscription_deleted_cancels(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "dave@example.com")
    _set_customer_id(uid, "cus_test_dave")

    # First make them ACTIVE
    _post_webhook(isolated, {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test_dave", "subscription": "sub_dave"}},
    })
    assert _read_status(uid)[0] == STATUS_ACTIVE

    # Then cancel
    _post_webhook(isolated, {
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_test_dave", "id": "sub_dave"}},
    })
    assert _read_status(uid)[0] == STATUS_CANCELED


def test_webhook_payment_failed_marks_past_due(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "eve@example.com")
    _set_customer_id(uid, "cus_test_eve")
    _post_webhook(isolated, {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test_eve", "subscription": "sub_eve"}},
    })

    _post_webhook(isolated, {
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_test_eve"}},
    })
    assert _read_status(uid)[0] == STATUS_PAST_DUE


def test_webhook_payment_succeeded_recovers_from_past_due(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "frank@example.com")
    _set_customer_id(uid, "cus_test_frank")
    _post_webhook(isolated, {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test_frank", "subscription": "sub_frank"}},
    })
    _post_webhook(isolated, {
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_test_frank"}},
    })
    assert _read_status(uid)[0] == STATUS_PAST_DUE

    _post_webhook(isolated, {
        "type": "invoice.payment_succeeded",
        "data": {"object": {
            "customer": "cus_test_frank",
            "current_period_end": int(
                datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
            ),
        }},
    })
    assert _read_status(uid)[0] == STATUS_ACTIVE


def test_webhook_unknown_event_returns_200_without_changing_state(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "greg@example.com")
    _set_customer_id(uid, "cus_test_greg")

    result = _post_webhook(isolated, {
        "type": "charge.succeeded",        # we don't react to this
        "data": {"object": {"customer": "cus_test_greg"}},
    })
    assert result["status"] == 200
    assert _read_status(uid)[0] == STATUS_TRIALING


def test_webhook_bad_signature_returns_400(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "hank@example.com")
    r = isolated.post(
        "/webhooks/stripe",
        content="{}",
        headers={"Stripe-Signature": FakeStripeAdapter.BAD_SIGNATURE},
    )
    assert r.status_code == 400


def test_active_user_can_reach_dashboard(monkeypatch):
    """End-to-end: after checkout.session.completed, the user can reach
    the gated dashboard."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "ivy@example.com")
    _set_customer_id(uid, "cus_test_ivy")

    # Force-expire trial to prove it's the subscription, not the trial,
    # that's granting access.
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.trial_ends_at = "2020-01-01T00:00:00+00:00"

    # Without checkout completion, dashboard redirects.
    assert isolated.get("/", follow_redirects=False).status_code == 303

    # Webhook flips status to ACTIVE.
    _post_webhook(isolated, {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_test_ivy", "subscription": "sub_ivy"}},
    })

    # Now dashboard is reachable.
    assert isolated.get("/", follow_redirects=False).status_code == 200
