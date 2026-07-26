"""Admin document publishing — env-var gate + upload/delete + parse-on-upload."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from nac_pay.app.main import app
from nac_pay.auth import find_by_email, get_email_sender, is_admin
from nac_pay.onboarding import mark_completed
from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

_FA_FIXTURE = "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"


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
    uid = find_by_email(email)
    assert uid is not None
    # Promote to ACTIVE so the subscription gate is satisfied; we're
    # specifically testing the admin gate, not billing.
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == uid)
        ).scalar_one()
        row.subscription_status = "ACTIVE"
    # The onboarding middleware would otherwise redirect this fresh user
    # away from /admin/documents (it isn't in the onboarding-public path
    # list, deliberately — admin isn't part of setup). Mark them past
    # onboarding so the admin routes are reachable in these tests.
    mark_completed(uid)
    return uid


def test_is_admin_matches_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Example.com, other@x.com")
    client = TestClient(app)
    uid = _signup_and_verify(client, "boss@example.com")
    assert is_admin(uid) is True
    uid2 = _signup_and_verify(TestClient(app), "pilot@example.com")
    assert is_admin(uid2) is False


def test_admin_routes_404_for_non_admin(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "pilot@example.com")
    assert client.get("/admin/documents").status_code == 404


def test_admin_upload_publishes_and_reports_pilots(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "boss@example.com")
    fa_bytes = (Path(__file__).resolve().parents[2] / "docs" / _FA_FIXTURE).read_bytes()
    r = client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD"},
        files={"upload": ("fa.pdf", fa_bytes, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/admin/documents?ym=2026-5")
    assert "fa.pdf" in page.text
    assert "pilot code" in page.text.lower()   # parse feedback rendered


def test_admin_upload_rejects_garbage_pdf(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "boss@example.com")
    r = client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD"},
        files={"upload": ("junk.pdf", b"not a pdf", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    from nac_pay.storage import SharedDocumentsStore, get_data_dir
    assert SharedDocumentsStore(get_data_dir()).list_final_awards(2026, 5) == []
