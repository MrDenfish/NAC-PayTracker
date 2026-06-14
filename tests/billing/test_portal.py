"""Stripe Customer Portal route + template button switching."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import get_email_sender
from nac_pay.billing import (
    FakeStripeAdapter,
    get_stripe_adapter,
    reset_stripe_adapter,
    snapshot,
)
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> str:
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)
    with session_scope() as sess:
        return sess.execute(
            select(UserRow.user_id).where(UserRow.email == email.lower())
        ).scalar_one()


def _set_customer_id(user_id: str, customer_id: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one()
        row.stripe_customer_id = customer_id


def _set_status(user_id: str, status: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one()
        row.subscription_status = status


@pytest.fixture(autouse=True)
def _isolated_stripe(monkeypatch):
    reset_stripe_adapter()
    monkeypatch.setenv("STRIPE_BACKEND", "fake")
    monkeypatch.setenv("BASE_URL", "https://app.test")
    yield
    reset_stripe_adapter()


# ── Snapshot exposes stripe_customer_id ─────────────────────────────


def test_snapshot_carries_customer_id_when_present():
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "alice@example.com")
    _set_customer_id(uid, "cus_test_alice")

    snap = snapshot(uid)
    assert snap.stripe_customer_id == "cus_test_alice"
    assert snap.can_open_portal is True


def test_snapshot_can_open_portal_false_when_no_customer_id():
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "bob@example.com")
    snap = snapshot(uid)
    assert snap.stripe_customer_id is None
    assert snap.can_open_portal is False


# ── /billing/portal route ────────────────────────────────────────────


def test_portal_route_redirects_to_stripe_portal_url(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "carol@example.com")
    _set_customer_id(uid, "cus_test_carol")

    r = isolated.post("/billing/portal", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://fake-stripe.test/portal/cus_test_carol"

    adapter: FakeStripeAdapter = get_stripe_adapter()  # type: ignore[assignment]
    assert adapter.state.last_portal_return_url == "https://app.test/billing"


def test_portal_route_redirects_to_billing_when_no_customer(monkeypatch):
    """Users who never reached Checkout don't have a Stripe Customer —
    /billing/portal must not 500 or hit Stripe; bounce back to /billing."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "dave@example.com")

    r = isolated.post("/billing/portal", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/billing?no_customer=1"


def test_portal_route_works_for_canceled_users_too(monkeypatch):
    """A CANCELED user can still re-open the portal to reactivate
    (Stripe lets them within the cancel window). Customer Portal access
    is gated by customer_id presence, not by status."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "eve@example.com")
    _set_customer_id(uid, "cus_test_eve")
    _set_status(uid, "CANCELED")

    r = isolated.post("/billing/portal", follow_redirects=False)
    assert r.status_code == 303
    assert "fake-stripe.test/portal" in r.headers["location"]


def test_portal_route_dev_mode_redirect(monkeypatch):
    """In dev mode (AUTH_REQUIRED=false), portal route bounces back
    with the dev banner — no Stripe call attempted."""
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    client = TestClient(app)
    r = client.post("/billing/portal", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/billing?dev_mode=1"


# ── /billing template button switching ─────────────────────────────


def test_billing_page_shows_add_payment_for_trialing_no_customer(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "frank@example.com")
    r = isolated.get("/billing")
    assert "Add payment" in r.text
    assert "Manage subscription" not in r.text


def test_billing_page_shows_manage_subscription_when_customer_exists(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "greg@example.com")
    _set_customer_id(uid, "cus_test_greg")
    _set_status(uid, "ACTIVE")

    r = isolated.get("/billing")
    assert "Manage subscription" in r.text
    # ACTIVE + has customer → no upgrade button visible
    assert "Add payment" not in r.text
    assert "Subscribe" not in r.text


def test_billing_page_shows_both_buttons_for_canceled_with_customer(monkeypatch):
    """CANCELED + customer_id → Manage subscription (portal) AND
    Re-subscribe (Checkout) — give the user both paths back."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    uid = _signup_and_verify(isolated, "hank@example.com")
    _set_customer_id(uid, "cus_test_hank")
    _set_status(uid, "CANCELED")

    r = isolated.get("/billing")
    assert "Manage subscription" in r.text
    assert "Re-subscribe" in r.text


def test_billing_no_customer_query_param_shows_banner(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    isolated = TestClient(app)
    _signup_and_verify(isolated, "ivy@example.com")
    r = isolated.get("/billing?no_customer=1")
    assert "No Stripe customer on file yet" in r.text
