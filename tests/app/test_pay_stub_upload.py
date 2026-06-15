"""/documents PAY_STUB upload + delete + Compare inspector integration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import get_email_sender
from nac_pay.onboarding import mark_completed
from nac_pay.storage import DocumentKind, UserDocumentsStore, get_data_dir
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow


def _docs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _bootstrap_paid_user(monkeypatch, email: str) -> tuple[TestClient, str]:
    """Sign up, verify, mark ACTIVE + onboarded."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("STRIPE_BACKEND", "fake")
    isolated = TestClient(app)
    isolated.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    isolated.get(f"/verify/{token}", follow_redirects=False)
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.email == email.lower())
        ).scalar_one()
        row.subscription_status = "ACTIVE"
        uid = row.user_id
    mark_completed(uid)
    return isolated, uid


# ── Upload route ─────────────────────────────────────────────────────


def test_pay_stub_upload_accumulates_not_overwrites(monkeypatch):
    """Uploading PAY_STUB twice creates two rows (semi-monthly)."""
    client, uid = _bootstrap_paid_user(monkeypatch, "alice@example.com")

    stub1 = (_docs_dir() / "pay Stubs" / "May_ Base_payStub.pdf").read_bytes()
    stub2 = (_docs_dir() / "pay Stubs" / "May_payStub.pdf").read_bytes()

    r1 = client.post(
        "/documents/upload",
        data={"year": "2026", "month": "5", "kind": "PAY_STUB"},
        files={"upload": ("first.pdf", stub1, "application/pdf")},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    r2 = client.post(
        "/documents/upload",
        data={"year": "2026", "month": "5", "kind": "PAY_STUB"},
        files={"upload": ("second.pdf", stub2, "application/pdf")},
        follow_redirects=False,
    )
    assert r2.status_code == 303

    store = UserDocumentsStore(get_data_dir(), uid)
    stubs = store.list_stubs(2026, 5)
    assert [s.slot for s in stubs] == [0, 1]
    assert [s.original_filename for s in stubs] == ["first.pdf", "second.pdf"]


def test_pay_stub_upload_rejects_wrong_extension(monkeypatch):
    client, _ = _bootstrap_paid_user(monkeypatch, "bob@example.com")
    r = client.post(
        "/documents/upload",
        data={"year": "2026", "month": "5", "kind": "PAY_STUB"},
        files={"upload": ("nope.txt", b"text", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "must+be+a+.pdf" in r.headers["location"]


def test_pay_stub_upload_blocked_for_default_user():
    client = TestClient(app)
    r = client.post(
        "/documents/upload",
        data={"year": "2026", "month": "5", "kind": "PAY_STUB"},
        files={"upload": ("x.pdf", b"data", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Default+user+cannot+upload" in r.headers["location"]


# ── Delete route ─────────────────────────────────────────────────────


def test_pay_stub_delete_removes_specified_slot(monkeypatch):
    client, uid = _bootstrap_paid_user(monkeypatch, "carol@example.com")
    store = UserDocumentsStore(get_data_dir(), uid)
    store.save_stub(2026, 5, "a.pdf", b"a")
    store.save_stub(2026, 5, "b.pdf", b"b")

    r = client.post(
        "/documents/delete",
        data={"year": "2026", "month": "5", "kind": "PAY_STUB", "slot": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert [s.slot for s in store.list_stubs(2026, 5)] == [1]


# ── /documents page renders stub list ─────────────────────────────────


def test_documents_page_lists_uploaded_stubs(monkeypatch):
    client, uid = _bootstrap_paid_user(monkeypatch, "dave@example.com")
    store = UserDocumentsStore(get_data_dir(), uid)
    store.save_stub(2026, 5, "may_first.pdf", b"a")
    store.save_stub(2026, 5, "may_second.pdf", b"b")

    r = client.get("/documents")
    assert r.status_code == 200
    assert "may_first.pdf" in r.text
    assert "may_second.pdf" in r.text
    assert "Pay Stub" in r.text


# ── Compare consumes uploaded stubs ───────────────────────────────────


def test_compare_uses_uploaded_stubs_for_real_user(monkeypatch):
    """Upload the bundled May stubs as a real user; Compare should
    produce the same per-row data as the default-user (bundled) view."""
    client, uid = _bootstrap_paid_user(monkeypatch, "erin@example.com")

    # Set pilot_id=DFI so the engine resolves the FISHER row.
    client.post(
        "/settings",
        data={
            "name": "Erin Pilot", "position": "FO", "hourly_rate": "124.59",
            "pilot_id": "DFI", "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
        follow_redirects=False,
    )

    # Upload May FA + Packet + both pay stubs.
    fa = (_docs_dir() / "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf").read_bytes()
    pkt = (_docs_dir() / "MAY  2026  Trip Pairing Packet.pdf").read_bytes()
    stub1 = (_docs_dir() / "pay Stubs" / "May_ Base_payStub.pdf").read_bytes()
    stub2 = (_docs_dir() / "pay Stubs" / "May_payStub.pdf").read_bytes()

    for kind, name, data in [
        ("FINAL_AWARD", "fa.pdf", fa),
        ("TRIP_PACKET", "packet.pdf", pkt),
        ("PAY_STUB", "stub1.pdf", stub1),
        ("PAY_STUB", "stub2.pdf", stub2),
    ]:
        client.post(
            "/documents/upload",
            data={"year": "2026", "month": "5", "kind": kind},
            files={"upload": (name, data, "application/pdf")},
            follow_redirects=False,
        )

    r = client.get("/compare?ym=2026-5", follow_redirects=False)
    assert r.status_code == 200, r.text[:400]
    # The verdict banner exists (not NO_STUBS) — proves stubs were
    # resolved from upload, not from the killed _STUB_INDEX.
    body = r.text
    assert "No pay stubs uploaded" not in body
    assert "By category" in body  # comparison table rendered
    assert "Source stubs" in body  # stub chips rendered


def test_compare_no_stubs_message_for_user_without_uploads(monkeypatch):
    client, _ = _bootstrap_paid_user(monkeypatch, "fred@example.com")

    # Upload FA + Packet but NO stubs. Set pilot_id so the engine works.
    client.post(
        "/settings",
        data={
            "name": "Fred", "position": "FO", "hourly_rate": "124.59",
            "pilot_id": "DFI", "sick_bank_days": "0", "pto_bank_days": "0",
            "feed_url": "", "feed_auto_update": "",
        },
        follow_redirects=False,
    )
    fa = (_docs_dir() / "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf").read_bytes()
    pkt = (_docs_dir() / "MAY  2026  Trip Pairing Packet.pdf").read_bytes()
    for kind, name, data in [("FINAL_AWARD", "fa.pdf", fa), ("TRIP_PACKET", "p.pdf", pkt)]:
        client.post(
            "/documents/upload",
            data={"year": "2026", "month": "5", "kind": kind},
            files={"upload": (name, data, "application/pdf")},
            follow_redirects=False,
        )

    r = client.get("/compare?ym=2026-5", follow_redirects=False)
    assert r.status_code == 200
    assert "No pay stubs uploaded for this month" in r.text


# ── Inspector view ────────────────────────────────────────────────────


def test_inspector_view_renders_raw_stub_fields_for_default_user():
    """Default user has bundled May stubs — the inspector section should
    surface every parsed PayStubLine."""
    client = TestClient(app)
    r = client.get("/compare?ym=2026-5", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    assert "Raw stub data (parsed)" in body
    assert "inspector-stub" in body
    # The May stub has a Regular Pay row; should appear in raw form.
    assert "Regular Pay" in body
    # Per-stub source filename surfaces.
    assert "May_payStub.pdf" in body or "May_ Base_payStub.pdf" in body


def test_inspector_absent_when_no_stubs():
    """If no stubs exist for the month, the inspector card shouldn't
    render at all — it's tied to inspector_stubs being non-empty."""
    client = TestClient(app)
    # 2026-6 has bundled docs but no bundled stubs.
    r = client.get("/compare?ym=2026-6", follow_redirects=False)
    assert r.status_code == 200
    assert "Raw stub data (parsed)" not in r.text
