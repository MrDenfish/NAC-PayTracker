"""UserDocumentsStore — disk + DB round-trip + multi-user isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from nac_pay.storage import (
    DocumentKind,
    UserDocumentsStore,
    expected_extension,
    get_data_dir,
)


def _docs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def _fa_bytes() -> bytes:
    return (_docs_dir() / "JUNE 2026 ANC 737 - FIRST OFFICER FINAL AWARDS.pdf").read_bytes()


def _packet_bytes() -> bytes:
    return (_docs_dir() / "JUNE 2026 Trip Pairing Packet.pdf").read_bytes()


def _ical_bytes() -> bytes:
    return (_docs_dir() / "iCal_schedule_feed.ics").read_bytes()


# ── Kind metadata ───────────────────────────────────────────────────


def test_expected_extension_per_kind():
    assert expected_extension(DocumentKind.FINAL_AWARD) == ".pdf"
    assert expected_extension(DocumentKind.TRIP_PACKET) == ".pdf"
    assert expected_extension(DocumentKind.ICAL_FEED) == ".ics"


# ── Round-trip ──────────────────────────────────────────────────────


def test_save_creates_disk_path_and_db_row():
    store = UserDocumentsStore(get_data_dir(), user_id="alice")
    rec = store.save(2026, 6, DocumentKind.FINAL_AWARD, "may.pdf", _fa_bytes())
    assert rec.path.exists()
    assert rec.original_filename == "may.pdf"

    loaded = store.get(2026, 6, DocumentKind.FINAL_AWARD)
    assert loaded is not None
    assert loaded.original_filename == "may.pdf"
    assert loaded.path == rec.path
    assert loaded.path.read_bytes() == _fa_bytes()


def test_save_overwrites_existing_slot():
    store = UserDocumentsStore(get_data_dir(), user_id="bob")
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "first.pdf", b"v1")
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "second.pdf", b"v2-newer")
    loaded = store.get(2026, 6, DocumentKind.FINAL_AWARD)
    assert loaded is not None
    assert loaded.original_filename == "second.pdf"
    assert loaded.path.read_bytes() == b"v2-newer"


def test_get_returns_none_when_absent():
    store = UserDocumentsStore(get_data_dir(), user_id="carol")
    assert store.get(2026, 5, DocumentKind.FINAL_AWARD) is None


def test_delete_removes_disk_and_db():
    store = UserDocumentsStore(get_data_dir(), user_id="dave")
    rec = store.save(2026, 6, DocumentKind.FINAL_AWARD, "x.pdf", b"data")
    assert rec.path.exists()
    deleted = store.delete(2026, 6, DocumentKind.FINAL_AWARD)
    assert deleted is True
    assert not rec.path.exists()
    assert store.get(2026, 6, DocumentKind.FINAL_AWARD) is None


def test_list_all_returns_every_uploaded_doc():
    store = UserDocumentsStore(get_data_dir(), user_id="eve")
    store.save(2026, 5, DocumentKind.FINAL_AWARD, "may_fa.pdf", b"a")
    store.save(2026, 5, DocumentKind.TRIP_PACKET, "may_pkt.pdf", b"b")
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "jun_fa.pdf", b"c")
    listing = store.list_all()
    assert len(listing) == 3
    kinds_by_month = {(r.year, r.month, r.kind) for r in listing}
    assert (2026, 5, DocumentKind.FINAL_AWARD) in kinds_by_month
    assert (2026, 5, DocumentKind.TRIP_PACKET) in kinds_by_month
    assert (2026, 6, DocumentKind.FINAL_AWARD) in kinds_by_month


def test_available_months_returns_distinct_sorted_desc():
    store = UserDocumentsStore(get_data_dir(), user_id="frank")
    store.save(2026, 4, DocumentKind.FINAL_AWARD, "apr.pdf", b"a")
    store.save(2026, 4, DocumentKind.TRIP_PACKET, "apr_pkt.pdf", b"b")
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "jun.pdf", b"c")
    store.save(2025, 12, DocumentKind.FINAL_AWARD, "dec.pdf", b"d")
    months = store.available_months()
    # Each month appears once even with multiple kinds; newest first.
    assert months == [(2026, 6), (2026, 4), (2025, 12)]


# ── Multi-user isolation ────────────────────────────────────────────


def test_two_users_have_separate_document_paths():
    alice = UserDocumentsStore(get_data_dir(), user_id="alice")
    bob = UserDocumentsStore(get_data_dir(), user_id="bob")
    alice.save(2026, 6, DocumentKind.FINAL_AWARD, "alice.pdf", b"alice-data")
    bob.save(2026, 6, DocumentKind.FINAL_AWARD, "bob.pdf", b"bob-data")

    alice_rec = alice.get(2026, 6, DocumentKind.FINAL_AWARD)
    bob_rec = bob.get(2026, 6, DocumentKind.FINAL_AWARD)
    assert alice_rec is not None and bob_rec is not None
    assert alice_rec.path != bob_rec.path
    assert alice_rec.path.read_bytes() == b"alice-data"
    assert bob_rec.path.read_bytes() == b"bob-data"
    # And the listing is scoped.
    assert len(alice.list_all()) == 1
    assert len(bob.list_all()) == 1


def test_delete_doesnt_remove_other_users_doc():
    alice = UserDocumentsStore(get_data_dir(), user_id="alice")
    bob = UserDocumentsStore(get_data_dir(), user_id="bob")
    alice.save(2026, 6, DocumentKind.FINAL_AWARD, "alice.pdf", b"alice-data")
    bob.save(2026, 6, DocumentKind.FINAL_AWARD, "bob.pdf", b"bob-data")
    alice.delete(2026, 6, DocumentKind.FINAL_AWARD)
    assert bob.get(2026, 6, DocumentKind.FINAL_AWARD) is not None
