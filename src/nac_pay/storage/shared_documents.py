"""Shared (admin-published) documents — disk + DB-row pair.

The site admin uploads each month's company documents once; every account
resolves them via ``services.documents_for_user`` (personal upload wins,
shared is the fallback). Only FINAL_AWARD (multi-slot: FO sheet + CA
sheet, or one combined PDF) and TRIP_PACKET (slot 0) may be shared — the
iCal feed is personal by definition and pay stubs are private.

Layout::

    {data_dir}/shared/docs/{year}-{month:02}/
        final_award_0.pdf
        final_award_1.pdf
        packet.pdf
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from .documents import DocumentKind

_SHARED_KINDS = {DocumentKind.FINAL_AWARD, DocumentKind.TRIP_PACKET}


@dataclass(frozen=True)
class SharedDocumentRecord:
    year: int
    month: int
    kind: DocumentKind
    slot: int
    path: Path
    original_filename: str
    uploaded_at: str
    uploaded_by: str
    size_bytes: int


class SharedDocumentsStore:
    """Site-wide document manager (no user scoping)."""

    def __init__(self, base_dir: Path):
        self._root = base_dir / "shared" / "docs"

    # ── Path resolution ────────────────────────────────────────────

    def _month_dir(self, year: int, month: int) -> Path:
        return self._root / f"{year}-{month:02}"

    def _path_for(self, year: int, month: int, kind: DocumentKind, slot: int) -> Path:
        name = f"final_award_{slot}.pdf" if kind is DocumentKind.FINAL_AWARD else "packet.pdf"
        return self._month_dir(year, month) / name

    @staticmethod
    def _check_kind(kind: DocumentKind) -> None:
        if kind not in _SHARED_KINDS:
            raise ValueError(f"{kind.value} cannot be shared — FA and Packet only.")

    # ── Public API: FINAL_AWARD (multi-slot, appends) ──────────────

    def save_final_award(
        self, year: int, month: int, original_filename: str, data: bytes,
        uploaded_by: str,
    ) -> SharedDocumentRecord:
        """Append a Final Award at the next available slot. Usually two
        uploads per month (FO + CA); occasionally one combined PDF."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with session_scope() as sess:
            used_slots = sess.execute(
                select(SharedDocumentRow.slot).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                    SharedDocumentRow.kind == DocumentKind.FINAL_AWARD.value,
                )
            ).scalars().all()
            slot = (max(used_slots) + 1) if used_slots else 0

            path = self._path_for(year, month, DocumentKind.FINAL_AWARD, slot)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

            sess.add(
                SharedDocumentRow(
                    year=year, month=month,
                    kind=DocumentKind.FINAL_AWARD.value, slot=slot,
                    original_filename=original_filename,
                    uploaded_at=uploaded_at,
                    uploaded_by=uploaded_by,
                    size_bytes=len(data),
                )
            )

        return SharedDocumentRecord(
            year=year, month=month, kind=DocumentKind.FINAL_AWARD,
            slot=slot, path=path,
            original_filename=original_filename, uploaded_at=uploaded_at,
            uploaded_by=uploaded_by, size_bytes=len(data),
        )

    def list_final_awards(self, year: int, month: int) -> list[SharedDocumentRecord]:
        """All Final Awards for the month, ordered by slot (upload order)."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(SharedDocumentRow).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                    SharedDocumentRow.kind == DocumentKind.FINAL_AWARD.value,
                ).order_by(SharedDocumentRow.slot)
            ).scalars().all()
            return [self._row_to_record(r) for r in rows]

    # ── Public API: TRIP_PACKET (slot 0, replaces) ─────────────────

    def save_packet(
        self, year: int, month: int, original_filename: str, data: bytes,
        uploaded_by: str,
    ) -> SharedDocumentRecord:
        """Save the Trip Pairing Packet at slot 0. Re-uploading replaces
        the previous file/row in place."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        path = self._path_for(year, month, DocumentKind.TRIP_PACKET, slot=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with session_scope() as sess:
            existing = sess.execute(
                select(SharedDocumentRow).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                    SharedDocumentRow.kind == DocumentKind.TRIP_PACKET.value,
                    SharedDocumentRow.slot == 0,
                )
            ).scalar_one_or_none()
            if existing is None:
                sess.add(
                    SharedDocumentRow(
                        year=year, month=month,
                        kind=DocumentKind.TRIP_PACKET.value, slot=0,
                        original_filename=original_filename,
                        uploaded_at=uploaded_at,
                        uploaded_by=uploaded_by,
                        size_bytes=len(data),
                    )
                )
            else:
                existing.original_filename = original_filename
                existing.uploaded_at = uploaded_at
                existing.uploaded_by = uploaded_by
                existing.size_bytes = len(data)

        return SharedDocumentRecord(
            year=year, month=month, kind=DocumentKind.TRIP_PACKET,
            slot=0, path=path,
            original_filename=original_filename, uploaded_at=uploaded_at,
            uploaded_by=uploaded_by, size_bytes=len(data),
        )

    def get_packet(self, year: int, month: int) -> SharedDocumentRecord | None:
        """Get the current Trip Pairing Packet for the month, if any."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        with session_scope() as sess:
            row = sess.execute(
                select(SharedDocumentRow).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                    SharedDocumentRow.kind == DocumentKind.TRIP_PACKET.value,
                    SharedDocumentRow.slot == 0,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_record(row)

    # ── Shared delete / bulk read ───────────────────────────────────

    def delete(
        self, year: int, month: int, kind: DocumentKind, slot: int = 0,
    ) -> bool:
        """Delete a shared document by (year, month, kind, slot). Only
        FINAL_AWARD and TRIP_PACKET may be shared."""
        self._check_kind(kind)
        from .db import session_scope
        from .db_models import SharedDocumentRow

        path = self._path_for(year, month, kind, slot)
        if path.exists():
            path.unlink()
        with session_scope() as sess:
            result = sess.execute(
                delete(SharedDocumentRow).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                    SharedDocumentRow.kind == kind.value,
                    SharedDocumentRow.slot == slot,
                )
            )
            return result.rowcount > 0

    def list_month(self, year: int, month: int) -> list[SharedDocumentRecord]:
        """All shared documents (any kind) for the month."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(SharedDocumentRow).where(
                    SharedDocumentRow.year == year,
                    SharedDocumentRow.month == month,
                ).order_by(SharedDocumentRow.kind, SharedDocumentRow.slot)
            ).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def months_with_full_set(self) -> list[tuple[int, int]]:
        """Distinct (year, month) tuples having at least one FINAL_AWARD
        AND a TRIP_PACKET. Sorted newest first to match the existing
        month-switcher ordering."""
        from .db import session_scope
        from .db_models import SharedDocumentRow

        with session_scope() as sess:
            rows = sess.execute(
                select(
                    SharedDocumentRow.year,
                    SharedDocumentRow.month,
                    SharedDocumentRow.kind,
                ).distinct()
            ).all()

        kinds_by_month: dict[tuple[int, int], set[str]] = {}
        for r in rows:
            kinds_by_month.setdefault((r.year, r.month), set()).add(r.kind)

        required = {DocumentKind.FINAL_AWARD.value, DocumentKind.TRIP_PACKET.value}
        out = [ym for ym, kinds in kinds_by_month.items() if required <= kinds]
        return sorted(out, reverse=True)

    def _row_to_record(self, r) -> SharedDocumentRecord:
        kind = DocumentKind(r.kind)
        return SharedDocumentRecord(
            year=r.year, month=r.month,
            kind=kind, slot=r.slot,
            path=self._path_for(r.year, r.month, kind, r.slot),
            original_filename=r.original_filename,
            uploaded_at=r.uploaded_at,
            uploaded_by=r.uploaded_by,
            size_bytes=r.size_bytes,
        )
