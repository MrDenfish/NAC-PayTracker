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


# Map kind → filename + accepted extension.
_KIND_FILENAMES: dict[DocumentKind, tuple[str, str]] = {
    DocumentKind.FINAL_AWARD: ("final_award.pdf", ".pdf"),
    DocumentKind.TRIP_PACKET: ("packet.pdf", ".pdf"),
    DocumentKind.ICAL_FEED: ("feed.ics", ".ics"),
}


@dataclass(frozen=True)
class DocumentRecord:
    user_id: str
    year: int
    month: int
    kind: DocumentKind
    path: Path
    original_filename: str
    uploaded_at: str

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

    def _path_for(self, year: int, month: int, kind: DocumentKind) -> Path:
        return self._month_dir(year, month) / _KIND_FILENAMES[kind][0]

    # ── Public API ─────────────────────────────────────────────────

    def save(
        self,
        year: int,
        month: int,
        kind: DocumentKind,
        original_filename: str,
        data: bytes,
    ) -> DocumentRecord:
        from .db import session_scope
        from .db_models import UserDocumentRow, UserRow

        path = self._path_for(year, month, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with session_scope() as sess:
            # Ensure the user exists (auth flow creates the row, but the
            # default user doesn't have one).
            user = sess.execute(
                select(UserRow).where(UserRow.user_id == self._user_id)
            ).scalar_one_or_none()
            if user is None:
                sess.add(UserRow(user_id=self._user_id))
                sess.flush()

            existing = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                )
            ).scalar_one_or_none()
            if existing is None:
                sess.add(
                    UserDocumentRow(
                        user_id=self._user_id,
                        year=year,
                        month=month,
                        kind=kind.value,
                        original_filename=original_filename,
                        uploaded_at=uploaded_at,
                    )
                )
            else:
                existing.original_filename = original_filename
                existing.uploaded_at = uploaded_at

        return DocumentRecord(
            user_id=self._user_id,
            year=year, month=month, kind=kind, path=path,
            original_filename=original_filename, uploaded_at=uploaded_at,
        )

    def get(
        self, year: int, month: int, kind: DocumentKind,
    ) -> DocumentRecord | None:
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            row = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return DocumentRecord(
                user_id=self._user_id,
                year=row.year, month=row.month,
                kind=DocumentKind(row.kind),
                path=self._path_for(row.year, row.month, DocumentKind(row.kind)),
                original_filename=row.original_filename,
                uploaded_at=row.uploaded_at,
            )

    def list_all(self) -> list[DocumentRecord]:
        from .db import session_scope
        from .db_models import UserDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id
                )
            ).scalars().all()
            return [
                DocumentRecord(
                    user_id=self._user_id,
                    year=r.year, month=r.month,
                    kind=DocumentKind(r.kind),
                    path=self._path_for(r.year, r.month, DocumentKind(r.kind)),
                    original_filename=r.original_filename,
                    uploaded_at=r.uploaded_at,
                )
                for r in rows
            ]

    def delete(self, year: int, month: int, kind: DocumentKind) -> bool:
        from .db import session_scope
        from .db_models import UserDocumentRow

        path = self._path_for(year, month, kind)
        if path.exists():
            path.unlink()
        with session_scope() as sess:
            result = sess.execute(
                delete(UserDocumentRow).where(
                    UserDocumentRow.user_id == self._user_id,
                    UserDocumentRow.year == year,
                    UserDocumentRow.month == month,
                    UserDocumentRow.kind == kind.value,
                )
            )
            return result.rowcount > 0

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
