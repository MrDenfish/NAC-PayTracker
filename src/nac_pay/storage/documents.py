"""Per-user uploaded documents — disk + DB-row pair.

Files are saved at deterministic paths so the pipeline can resolve them
quickly. The DB row carries metadata (original filename, upload time)
so the UI can show "May FA uploaded by you on 2026-06-12" rather than
just "FA: yes".

Layout::

    {data_dir}/users/{user_id}/docs/{year}-{month:02}/
        final_award.pdf
        packet.pdf
        feed.ics

Re-uploading replaces the previous file at the same path; the DB row
is updated in place via the composite PK ``(user_id, year, month, kind)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select


class DocumentKind(StrEnum):
    FINAL_AWARD = "FINAL_AWARD"
    TRIP_PACKET = "TRIP_PACKET"
    ICAL_FEED = "ICAL_FEED"
    PAY_STUB = "PAY_STUB"


# Map kind → (filename template, accepted extension). FA/Packet/iCal use
# their fixed canonical names (slot is always 0 — re-upload replaces in
# place). PAY_STUB templates by slot index so semi-monthly stubs
# accumulate side by side.
_KIND_FILENAMES: dict[DocumentKind, tuple[str, str]] = {
    DocumentKind.FINAL_AWARD: ("final_award.pdf", ".pdf"),
    DocumentKind.TRIP_PACKET: ("packet.pdf", ".pdf"),
    DocumentKind.ICAL_FEED: ("feed.ics", ".ics"),
    DocumentKind.PAY_STUB: ("stub_{slot}.pdf", ".pdf"),
}


def _filename_for(kind: DocumentKind, slot: int) -> str:
    template = _KIND_FILENAMES[kind][0]
    return template.format(slot=slot) if "{slot}" in template else template


@dataclass(frozen=True)
class DocumentRecord:
    user_id: str
    year: int
    month: int
    kind: DocumentKind
    path: Path
    original_filename: str
    uploaded_at: str
    slot: int = 0

    @property
    def exists(self) -> bool:
        return self.path.exists()


def expected_extension(kind: DocumentKind) -> str:
    return _KIND_FILENAMES[kind][1]


class UserDocumentsStore:
    """Per-user document manager."""

    def __init__(self, base_dir: Path, user_id: str):
        from .users import user_dir
        self._base_dir = base_dir
        self._user_id = user_id
        self._user_root = user_dir(base_dir, user_id) / "docs"

    # ── Path resolution ────────────────────────────────────────────

    def _month_dir(self, year: int, month: int) -> Path:
        return self._user_root / f"{year}-{month:02}"

    def _path_for(
        self, year: int, month: int, kind: DocumentKind, slot: int = 0,
    ) -> Path:
        return self._month_dir(year, month) / _filename_for(kind, slot)

    def _ensure_user_row(self, sess) -> None:
        """The auth flow creates the user row, but the default (dev) user
        doesn't have one — create on demand so the FK holds."""
        from .db_models import UserRow
        user = sess.execute(
            select(UserRow).where(UserRow.user_id == self._user_id)
        ).scalar_one_or_none()
        if user is None:
            sess.add(UserRow(user_id=self._user_id))
            sess.flush()

    # ── Public API: single-slot kinds (FA / Packet / iCal) ─────────

    def save(
        self,
        year: int,
        month: int,
        kind: DocumentKind,
        original_filename: str,
        data: bytes,
    ) -> DocumentRecord:
        """Save a one-per-month document (FA / Packet / iCal). Re-uploading
        replaces the file at slot=0. Use ``save_stub`` for pay stubs."""
        if kind is DocumentKind.PAY_STUB:
            raise ValueError("Use save_stub() for PAY_STUB — it appends, not replaces.")
        from .db import session_scope
        from .db_models import UserDocumentRow

        path = self._path_for(year, month, kind, slot=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with session_scope() as sess:
            self._ensure_user_row(sess)
            existing = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                    UserDocumentRow.slot == 0,
                )
            ).scalar_one_or_none()
            if existing is None:
                sess.add(
                    UserDocumentRow(
                        user_id=self._user_id,
                        year=year, month=month, kind=kind.value, slot=0,
                        original_filename=original_filename,
                        uploaded_at=uploaded_at,
                    )
                )
            else:
                existing.original_filename = original_filename
                existing.uploaded_at = uploaded_at

        return DocumentRecord(
            user_id=self._user_id,
            year=year, month=month, kind=kind, path=path, slot=0,
            original_filename=original_filename, uploaded_at=uploaded_at,
        )

    def get(
        self, year: int, month: int, kind: DocumentKind,
    ) -> DocumentRecord | None:
        """Get the canonical record for a one-per-month kind. For
        PAY_STUB, use ``list_stubs`` — there is no single record."""
        if kind is DocumentKind.PAY_STUB:
            raise ValueError("Use list_stubs() for PAY_STUB — multiple stubs per month.")
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            row = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                    UserDocumentRow.slot == 0,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_record(row)

    def delete(self, year: int, month: int, kind: DocumentKind) -> bool:
        """Delete a one-per-month document. For PAY_STUB, use
        ``delete_stub`` to specify which slot."""
        if kind is DocumentKind.PAY_STUB:
            raise ValueError("Use delete_stub() for PAY_STUB — multiple stubs per month.")
        from .db import session_scope
        from .db_models import UserDocumentRow

        path = self._path_for(year, month, kind, slot=0)
        if path.exists():
            path.unlink()
        with session_scope() as sess:
            result = sess.execute(
                delete(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                    UserDocumentRow.slot == 0,
                )
            )
            return result.rowcount > 0

    # ── Public API: pay stubs (multi-slot) ─────────────────────────

    def save_stub(
        self, year: int, month: int, original_filename: str, data: bytes,
    ) -> DocumentRecord:
        """Append a pay stub at the next available slot. Semi-monthly
        stubs (typically two per month) accumulate side by side."""
        from .db import session_scope
        from .db_models import UserDocumentRow

        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with session_scope() as sess:
            self._ensure_user_row(sess)
            used_slots = sess.execute(
                select(UserDocumentRow.slot).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == DocumentKind.PAY_STUB.value,
                )
            ).scalars().all()
            slot = (max(used_slots) + 1) if used_slots else 0

            path = self._path_for(year, month, DocumentKind.PAY_STUB, slot)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

            sess.add(
                UserDocumentRow(
                    user_id=self._user_id,
                    year=year, month=month,
                    kind=DocumentKind.PAY_STUB.value, slot=slot,
                    original_filename=original_filename,
                    uploaded_at=uploaded_at,
                )
            )

        return DocumentRecord(
            user_id=self._user_id,
            year=year, month=month, kind=DocumentKind.PAY_STUB,
            path=path, slot=slot,
            original_filename=original_filename, uploaded_at=uploaded_at,
        )

    def list_stubs(self, year: int, month: int) -> list[DocumentRecord]:
        """All pay stubs for the month, ordered by slot (upload order)."""
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == DocumentKind.PAY_STUB.value,
                ).order_by(UserDocumentRow.slot)
            ).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def delete_stub(self, year: int, month: int, slot: int) -> bool:
        """Delete a single pay stub by slot. Other slots are untouched —
        we don't renumber so existing handles stay valid."""
        from .db import session_scope
        from .db_models import UserDocumentRow

        path = self._path_for(year, month, DocumentKind.PAY_STUB, slot)
        if path.exists():
            path.unlink()
        with session_scope() as sess:
            result = sess.execute(
                delete(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == DocumentKind.PAY_STUB.value,
                    UserDocumentRow.slot == slot,
                )
            )
            return result.rowcount > 0

    # ── Bulk read ──────────────────────────────────────────────────

    def list_all(self) -> list[DocumentRecord]:
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id
                )
            ).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, r) -> DocumentRecord:
        kind = DocumentKind(r.kind)
        return DocumentRecord(
            user_id=self._user_id,
            year=r.year, month=r.month,
            kind=kind, slot=r.slot,
            path=self._path_for(r.year, r.month, kind, r.slot),
            original_filename=r.original_filename,
            uploaded_at=r.uploaded_at,
        )

    def available_months(self) -> list[tuple[int, int]]:
        """Distinct (year, month) tuples that have at least one document.
        Sorted newest first to match the existing month-switcher ordering."""
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserDocumentRow.year, UserDocumentRow.month)
                .where(UserDocumentRow.user_id == self._user_id)
                .distinct()
            ).all()
        out = sorted({(r.year, r.month) for r in rows}, reverse=True)
        return out
