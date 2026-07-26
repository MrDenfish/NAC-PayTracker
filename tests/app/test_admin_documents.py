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
_PACKET_FIXTURE = "MAY  2026  Trip Pairing Packet.pdf"


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


def test_admin_upload_rejects_garbage_packet_preserves_published_good_one(monkeypatch):
    """Finding 1 regression: parse-on-upload used to save-then-delete, so a
    bad re-upload destroyed the good published packet for every pilot
    before the parse check even ran. The fix validates a temp file first
    and only touches the store on success — a rejected re-upload must
    leave the previously published good packet (and the pipeline for that
    month) completely intact."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    uid = _signup_and_verify(client, "boss@example.com")

    fa_bytes = (Path(__file__).resolve().parents[2] / "docs" / _FA_FIXTURE).read_bytes()
    packet_bytes = (Path(__file__).resolve().parents[2] / "docs" / _PACKET_FIXTURE).read_bytes()

    client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD"},
        files={"upload": ("fa.pdf", fa_bytes, "application/pdf")},
        follow_redirects=False,
    )
    r = client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "TRIP_PACKET"},
        files={"upload": ("good-packet.pdf", packet_bytes, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error" not in r.headers["location"]

    from nac_pay.storage import SharedDocumentsStore, get_data_dir
    store = SharedDocumentsStore(get_data_dir())
    good = store.get_packet(2026, 5)
    assert good is not None
    assert good.original_filename == "good-packet.pdf"
    assert good.size_bytes == len(packet_bytes)

    r2 = client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "TRIP_PACKET"},
        files={"upload": ("junk.pdf", b"not a pdf", "application/pdf")},
        follow_redirects=False,
    )
    assert r2.status_code == 303 and "error=" in r2.headers["location"]

    still_good = store.get_packet(2026, 5)
    assert still_good is not None
    assert still_good.original_filename == "good-packet.pdf"
    assert still_good.size_bytes == len(packet_bytes)

    # The pipeline for that month must still resolve — not choke on a
    # deleted/half-replaced packet.
    from nac_pay.app.services import invalidate_caches, load_calendar
    invalidate_caches()
    data = load_calendar(2026, 5, uid)
    assert data is not None


def test_admin_delete_removes_published_slot(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "boss@example.com")
    fa_bytes = (Path(__file__).resolve().parents[2] / "docs" / _FA_FIXTURE).read_bytes()
    client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD"},
        files={"upload": ("fa.pdf", fa_bytes, "application/pdf")},
        follow_redirects=False,
    )
    from nac_pay.storage import SharedDocumentsStore, get_data_dir
    store = SharedDocumentsStore(get_data_dir())
    assert len(store.list_final_awards(2026, 5)) == 1

    r = client.post(
        "/admin/documents/delete",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD", "slot": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert store.list_final_awards(2026, 5) == []
    page = client.get("/admin/documents?ym=2026-5")
    assert "fa.pdf" not in page.text


def test_admin_delete_rejects_non_shareable_kind(monkeypatch):
    """Regression for eb44055: SharedDocumentsStore.delete raises a bare
    ValueError for a kind outside {FINAL_AWARD, TRIP_PACKET} (e.g.
    ICAL_FEED); the route must turn that into a clean 400, not a 500."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "boss@example.com")
    r = client.post(
        "/admin/documents/delete",
        data={"year": "2026", "month": "5", "kind": "ICAL_FEED", "slot": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_admin_delete_rejects_nonzero_slot_for_packet(monkeypatch):
    """Finding 2 regression: TRIP_PACKET is always stored at slot 0
    (SharedDocumentsStore._path_for ignores slot for packets), so a
    free-form slot!=0 on the delete route would unlink packet.pdf on disk
    while the DB delete matches 0 rows — an orphaned row/file desync. The
    route must reject slot!=0 for TRIP_PACKET with a 400, and the good
    packet must survive."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "boss@example.com")
    packet_bytes = (Path(__file__).resolve().parents[2] / "docs" / _PACKET_FIXTURE).read_bytes()
    client.post(
        "/admin/documents/upload",
        data={"year": "2026", "month": "5", "kind": "TRIP_PACKET"},
        files={"upload": ("good-packet.pdf", packet_bytes, "application/pdf")},
        follow_redirects=False,
    )
    r = client.post(
        "/admin/documents/delete",
        data={"year": "2026", "month": "5", "kind": "TRIP_PACKET", "slot": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 400

    from nac_pay.storage import SharedDocumentsStore, get_data_dir
    good = SharedDocumentsStore(get_data_dir()).get_packet(2026, 5)
    assert good is not None
    assert good.original_filename == "good-packet.pdf"


def test_admin_delete_404_for_non_admin(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    client = TestClient(app)
    _signup_and_verify(client, "pilot@example.com")
    r = client.post(
        "/admin/documents/delete",
        data={"year": "2026", "month": "5", "kind": "FINAL_AWARD", "slot": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 404
