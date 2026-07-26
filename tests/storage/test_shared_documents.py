"""Shared (admin-published) documents — disk + DB-row pair."""

from __future__ import annotations

from nac_pay.storage import DocumentKind, get_data_dir
from nac_pay.storage.shared_documents import SharedDocumentsStore


def _store() -> SharedDocumentsStore:
    return SharedDocumentsStore(get_data_dir())


def test_final_award_appends_slots():
    s = _store()
    r0 = s.save_final_award(2026, 8, "FA - FO.pdf", b"%PDF-fo", uploaded_by="u_admin")
    r1 = s.save_final_award(2026, 8, "FA - CA.pdf", b"%PDF-ca", uploaded_by="u_admin")
    assert (r0.slot, r1.slot) == (0, 1)
    assert r0.path.read_bytes() == b"%PDF-fo"
    assert r1.path.name == "final_award_1.pdf"
    listed = s.list_final_awards(2026, 8)
    assert [r.original_filename for r in listed] == ["FA - FO.pdf", "FA - CA.pdf"]
    assert listed[0].size_bytes == len(b"%PDF-fo")


def test_packet_slot0_replaces():
    s = _store()
    s.save_packet(2026, 8, "packet-v1.pdf", b"%PDF-1", uploaded_by="u_admin")
    r = s.save_packet(2026, 8, "packet-v2.pdf", b"%PDF-2", uploaded_by="u_admin")
    assert r.slot == 0
    assert s.get_packet(2026, 8).original_filename == "packet-v2.pdf"
    assert r.path.read_bytes() == b"%PDF-2"


def test_delete_removes_row_and_file():
    s = _store()
    r = s.save_final_award(2026, 8, "fa.pdf", b"%PDF", uploaded_by="u_admin")
    assert s.delete(2026, 8, DocumentKind.FINAL_AWARD, slot=0) is True
    assert not r.path.exists()
    assert s.list_final_awards(2026, 8) == []
    assert s.delete(2026, 8, DocumentKind.FINAL_AWARD, slot=0) is False


def test_months_with_full_set_requires_fa_and_packet():
    s = _store()
    s.save_final_award(2026, 8, "fa.pdf", b"%PDF", uploaded_by="u_admin")
    assert s.months_with_full_set() == []          # FA alone is not enough
    s.save_packet(2026, 8, "p.pdf", b"%PDF", uploaded_by="u_admin")
    s.save_final_award(2026, 9, "fa9.pdf", b"%PDF", uploaded_by="u_admin")
    s.save_packet(2026, 9, "p9.pdf", b"%PDF", uploaded_by="u_admin")
    assert s.months_with_full_set() == [(2026, 9), (2026, 8)]  # newest first


def test_shared_store_rejects_non_shared_kinds():
    import pytest
    s = _store()
    with pytest.raises(ValueError):
        s.delete(2026, 8, DocumentKind.ICAL_FEED)
