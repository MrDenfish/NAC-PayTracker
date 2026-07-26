"""In-app PDF viewing (personal-first, shared fallback) + the
"Provided by the site" section on the Documents page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.services import invalidate_caches
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
    SharedDocumentsStore,
    UserDocumentsStore,
    get_data_dir,
)


def _publish_shared_packet(year=2026, month=5, data=b"%PDF-shared-packet"):
    s = SharedDocumentsStore(get_data_dir())
    s.save_packet(year, month, "packet-shared.pdf", data, uploaded_by="admin")
    invalidate_caches()
    return s


def _publish_shared_fa(year, month, data, uploaded_by="admin"):
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", data, uploaded_by=uploaded_by)
    invalidate_caches()
    return s


def test_view_streams_shared_packet():
    _publish_shared_packet()
    client = TestClient(app)
    r = client.get("/documents/view/2026/5/TRIP_PACKET")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert "inline" in r.headers["content-disposition"]
    assert r.content == b"%PDF-shared-packet"


def test_view_personal_beats_shared():
    _publish_shared_fa(2026, 5, b"%PDF-shared-fa")
    # Write a personal FA directly under the default (no-auth) user — the
    # view route resolves per-request user_id the same way, so a personal
    # upload for that user_id must win over the shared fallback.
    store = UserDocumentsStore(get_data_dir(), DEFAULT_USER_ID)
    store.save(2026, 5, DocumentKind.FINAL_AWARD, "mine.pdf", b"%PDF-personal-fa")
    invalidate_caches()

    client = TestClient(app)
    r = client.get("/documents/view/2026/5/FINAL_AWARD")
    assert r.status_code == 200
    assert r.content == b"%PDF-personal-fa"


def test_view_404_when_missing():
    client = TestClient(app)
    assert client.get("/documents/view/2031/1/FINAL_AWARD").status_code == 404


def test_view_404_for_unknown_kind():
    client = TestClient(app)
    assert client.get("/documents/view/2026/5/NOT_A_KIND").status_code == 404


def test_view_404_for_non_shareable_kind():
    # ICAL_FEED is a real DocumentKind but is never viewable via this route.
    client = TestClient(app)
    assert client.get("/documents/view/2026/5/ICAL_FEED").status_code == 404


def test_view_fa_slot_selects_file():
    _publish_shared_fa(2026, 5, b"%PDF-fa-slot-0")
    _publish_shared_fa(2026, 5, b"%PDF-fa-slot-1")
    client = TestClient(app)
    r0 = client.get("/documents/view/2026/5/FINAL_AWARD/0")
    r1 = client.get("/documents/view/2026/5/FINAL_AWARD/1")
    assert r0.status_code == 200 and r0.content == b"%PDF-fa-slot-0"
    assert r1.status_code == 200 and r1.content == b"%PDF-fa-slot-1"


def test_documents_page_lists_shared_docs_with_view_links():
    _publish_shared_packet()
    _publish_shared_fa(2026, 5, b"%PDF-shared-fa")
    client = TestClient(app)
    r = client.get("/documents")
    assert r.status_code == 200
    assert "Provided by the site" in r.text
    assert 'href="/documents/view/2026/5/TRIP_PACKET"' in r.text
    assert 'href="/documents/view/2026/5/FINAL_AWARD/0"' in r.text
