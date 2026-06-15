"""Documents page + upload/delete routes + end-to-end pipeline with
uploaded docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import _pipeline, invalidate_caches
from nac_pay.auth import get_email_sender
from nac_pay.storage import (
    DocumentKind,
    UserDocumentsStore,
    get_data_dir,
)


def _docs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def _verify_token(body: str) -> str:
    m = re.search(r"/verify/([A-Za-z0-9_-]+)", body)
    assert m
    return m.group(1)


def _signup_and_verify(client: TestClient, email: str) -> None:
    client.post(
        "/signup",
        data={"email": email, "password": "long enough password", "confirm": "long enough password"},
        follow_redirects=False,
    )
    token = _verify_token(get_email_sender().sent[-1].body)
    client.get(f"/verify/{token}", follow_redirects=False)


def _bootstrap_paid_user(monkeypatch, email: str) -> TestClient:
    """Sign up, verify, grant ACTIVE subscription, mark onboarding done.
    These tests assert document-upload behavior; they're not testing the
    onboarding redirect."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("STRIPE_BACKEND", "fake")
    isolated = TestClient(app)
    _signup_and_verify(isolated, email)
    from sqlalchemy import select
    from nac_pay.onboarding import mark_completed
    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserRow
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.email == email.lower())
        ).scalar_one()
        row.subscription_status = "ACTIVE"
        uid = row.user_id
    mark_completed(uid)
    return isolated


# ── /documents page ──────────────────────────────────────────────────


def test_documents_page_renders_for_default_user_in_dev_mode():
    """Default dev user can view the page (sees the dev banner)."""
    client = TestClient(app)
    r = client.get("/documents")
    assert r.status_code == 200
    assert "Documents" in r.text
    assert "Default dev user" in r.text


def test_documents_page_for_new_real_user_shows_empty_state(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "alice@example.com")
    r = isolated.get("/documents")
    assert r.status_code == 200
    assert "No documents uploaded yet" in r.text


# ── Upload route ─────────────────────────────────────────────────────


def test_upload_pdf_writes_to_disk_and_db(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "bob@example.com")
    fa_bytes = (_docs_dir() / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf").read_bytes()

    r = isolated.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        files={"upload": ("june_fa.pdf", fa_bytes, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "uploaded=2026-6-FINAL_AWARD" in r.headers["location"]

    # Disk + DB reflect the upload.
    from nac_pay.storage.db import session_scope
    from nac_pay.storage.db_models import UserRow
    from sqlalchemy import select
    with session_scope() as sess:
        uid = sess.execute(
            select(UserRow.user_id).where(UserRow.email == "bob@example.com")
        ).scalar_one()
    store = UserDocumentsStore(get_data_dir(), uid)
    rec = store.get(2026, 6, DocumentKind.FINAL_AWARD)
    assert rec is not None
    assert rec.original_filename == "june_fa.pdf"
    assert rec.path.read_bytes() == fa_bytes


def test_upload_rejects_wrong_extension(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "carol@example.com")
    r = isolated.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        files={"upload": ("oops.txt", b"not a pdf", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "must+be+a+.pdf" in r.headers["location"]


def test_upload_rejects_empty_file(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "dave@example.com")
    r = isolated.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        files={"upload": ("empty.pdf", b"", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Empty+upload" in r.headers["location"]


def test_upload_rejects_invalid_month(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "eve@example.com")
    r = isolated.post(
        "/documents/upload",
        data={"year": "2026", "month": "13", "kind": "FINAL_AWARD"},
        files={"upload": ("x.pdf", b"data", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Invalid+month" in r.headers["location"]


def test_upload_blocked_for_default_user():
    client = TestClient(app)
    r = client.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        files={"upload": ("x.pdf", b"data", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Default+user+cannot+upload" in r.headers["location"]


# ── Delete route ─────────────────────────────────────────────────────


def test_delete_removes_uploaded_doc(monkeypatch):
    isolated = _bootstrap_paid_user(monkeypatch, "frank@example.com")
    isolated.post(
        "/documents/upload",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        files={"upload": ("f.pdf", b"data", "application/pdf")},
        follow_redirects=False,
    )
    r = isolated.post(
        "/documents/delete",
        data={"year": "2026", "month": "6", "kind": "FINAL_AWARD"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "deleted=2026-6-FINAL_AWARD" in r.headers["location"]


# ── End-to-end: uploaded docs drive the pipeline ─────────────────────


def test_uploaded_docs_run_the_full_pipeline(monkeypatch):
    """Sign up a real user, set pilot_id=DFI (so the parser finds the
    bundled FISHER row), upload the bundled FA + Packet + iCal as that
    user, and verify the engine computes pay from THOSE uploaded files."""
    isolated = _bootstrap_paid_user(monkeypatch, "greg@example.com")

    # Set pilot_id via Settings.
    isolated.post(
        "/settings",
        data={
            "name": "Greg Pilot",
            "position": "FO",
            "hourly_rate": "124.59",
            "pilot_id": "DFI",
            "sick_bank_days": "0",
            "pto_bank_days": "0",
            "feed_url": "",
            "feed_auto_update": "",
        },
        follow_redirects=False,
    )

    # Upload June docs.
    for kind, filename, source in [
        ("FINAL_AWARD", "fa.pdf", "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf"),
        ("TRIP_PACKET", "packet.pdf", "JUNE 2026 Trip Pairing Packet.pdf"),
        ("ICAL_FEED", "feed.ics", "iCal_schedule_feed.ics"),
    ]:
        isolated.post(
            "/documents/upload",
            data={"year": "2026", "month": "6", "kind": kind},
            files={"upload": (filename, (_docs_dir() / source).read_bytes(),
                              "application/octet-stream")},
            follow_redirects=False,
        )

    # Dashboard should now resolve via greg's uploaded docs.
    r = isolated.get("/?ym=2026-6", follow_redirects=False)
    assert r.status_code == 200, r.text[:400]
    # FISHER's June total — proves the engine ran against greg's
    # (now-uploaded) copy of the source files.
    assert "$8,195.53" in r.text
