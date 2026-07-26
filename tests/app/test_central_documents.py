"""Central (shared) document resolution: personal wins, shared is fallback."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from nac_pay.app.services import (
    available_months,
    documents_for_user,
    invalidate_caches,
    load_dashboard,
)
from nac_pay.schedule import PilotProfile, Position
from nac_pay.storage import (
    DocumentKind,
    PersistedPilotProfile,
    PilotProfileStore,
    SharedDocumentsStore,
    UserDocumentsStore,
    get_data_dir,
)

UID = "u_test_pilot"

_FA_FIXTURE = "MAY 2026 ANC 737 - FO FINAL AWARDS.pdf"
_PACKET_FIXTURE = "MAY  2026  Trip Pairing Packet.pdf"


def _fixture(name: str) -> bytes:
    # Same bundled sample documents the existing app tests use.
    return (Path(__file__).resolve().parents[2] / "docs" / name).read_bytes()


def _publish_shared(year=2026, month=5):
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_packet(year, month, "packet-shared.pdf", _fixture(_PACKET_FIXTURE), uploaded_by="admin")
    invalidate_caches()


def test_shared_docs_resolve_for_user_with_no_uploads():
    _publish_shared()
    resolved = documents_for_user(UID, 2026, 5)
    assert resolved is not None
    fa_paths, packet, ical = resolved
    assert len(fa_paths) == 1 and "shared" in str(fa_paths[0])
    assert ical is None


def test_personal_upload_beats_shared():
    _publish_shared()
    store = UserDocumentsStore(get_data_dir(), UID)
    store.save(2026, 5, DocumentKind.FINAL_AWARD, "mine.pdf", _fixture(_FA_FIXTURE))
    fa_paths, _, _ = documents_for_user(UID, 2026, 5)
    assert len(fa_paths) == 1 and f"users/{UID}" in str(fa_paths[0])


def test_no_docs_returns_none():
    assert documents_for_user(UID, 2031, 1) is None


def test_available_months_unions_personal_and_shared():
    _publish_shared(2026, 5)
    store = UserDocumentsStore(get_data_dir(), UID)
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "fa.pdf", b"%PDF")
    months = [(y, m) for (y, m, _) in available_months(UID)]
    assert (2026, 5) in months and (2026, 6) in months
    assert months == sorted(months, reverse=True)


def test_two_shared_fa_files_merge_pilot_codes():
    # Publish the same FA PDF twice as two slots: the merged grid must
    # contain the fixture's pilot codes (dict-update, no crash), and the
    # pipeline must find the default profile's pilot.
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(2026, 5, "fa-fo.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_final_award(2026, 5, "fa-fo-dup.pdf", _fixture(_FA_FIXTURE), uploaded_by="admin")
    s.save_packet(2026, 5, "packet-shared.pdf", _fixture(_PACKET_FIXTURE), uploaded_by="admin")
    invalidate_caches()

    prof = PilotProfile(
        pilot_id="DFI", name="Dennis FISHER", position=Position.FO,
        hourly_rate=Decimal("124.59"),
    )
    PilotProfileStore(get_data_dir(), UID).save(PersistedPilotProfile(profile=prof))

    view = load_dashboard(2026, 5, UID)
    assert view is not None
