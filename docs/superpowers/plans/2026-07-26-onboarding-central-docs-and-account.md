# Onboarding Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Central admin-published monthly documents, feed-link onboarding with pilot-code assist, account deletion, in-app PDF viewing, and HTML empty states — per the approved spec `docs/superpowers/specs/2026-07-26-onboarding-central-docs-and-account-design.md`.

**Architecture:** A new `SharedDocumentsStore` (files + `shared_documents` table) mirrors the existing per-user store; `services.documents_for_user()` resolves personal-first-then-shared and the pipeline merges multi-file Final Awards by pilot code. Admin surface is an env-var allowlist (`ADMIN_EMAILS`) gating `/admin/documents`. Onboarding step 2 becomes a feed-URL form reusing `pilot_profiles.feed_url` + the existing updater fetch path.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 (server-rendered, NO HTMX — plain full-page GETs + small vanilla JS), SQLAlchemy 2.0 (SQLite prod), pytest (+xdist), pdfplumber parsers (already built).

## Global Constraints

- Work on branch `feat/onboarding-central-docs` (already created; spec committed on it). Never commit to `main`; land via PR.
- TDD every task: write the failing test, see it fail, implement, see it pass, commit.
- Verify tests with a SINGLE file in foreground (e.g. `pytest tests/storage/test_shared_documents.py -v`); the full suite ONLY via `run_in_background` with `-n auto`. Always check pytest's own exit code — never a piped command's (`pytest ... > out.txt 2>&1; echo "RC=$?"`).
- pytest is already hermetic (conftest sets `NAC_PAY_DATA_DIR`); any one-off script MUST set `NAC_PAY_DATA_DIR=$(mktemp -d)` or it will wipe the author's dev DB.
- New DB objects are new TABLES only (`create_all` creates them); adding a column to an EXISTING table requires `_ensure_added_columns` in `storage/db.py` — this plan needs no such column.
- Match existing code style: docstring-first modules, `from __future__ import annotations`, lazy imports inside storage methods, frozen dataclasses for records.
- Copy exact user-facing strings from this plan verbatim (they were approved in the spec).
- `date.today()` / `datetime.now(timezone.utc)` are fine in app code (existing pattern) — the no-wall-clock rule applies to Workflow scripts, not this codebase.

---

### Task 1: `SharedDocumentRow` + `SharedDocumentsStore`

**Files:**
- Modify: `src/nac_pay/storage/db_models.py` (append new model)
- Create: `src/nac_pay/storage/shared_documents.py`
- Modify: `src/nac_pay/storage/__init__.py` (export)
- Test: `tests/storage/test_shared_documents.py` (new)

**Interfaces:**
- Consumes: `DocumentKind`, `get_data_dir`, `session_scope`, `Base` (all existing).
- Produces (later tasks rely on these exact names):
  - `SharedDocumentRecord` frozen dataclass: `year: int, month: int, kind: DocumentKind, slot: int, path: Path, original_filename: str, uploaded_at: str, uploaded_by: str, size_bytes: int`
  - `SharedDocumentsStore(base_dir: Path)` with methods:
    - `save_final_award(year, month, original_filename, data: bytes, uploaded_by: str) -> SharedDocumentRecord` — appends at next free slot
    - `save_packet(year, month, original_filename, data: bytes, uploaded_by: str) -> SharedDocumentRecord` — slot 0, re-upload replaces
    - `list_final_awards(year, month) -> list[SharedDocumentRecord]` — ordered by slot
    - `get_packet(year, month) -> SharedDocumentRecord | None`
    - `delete(year, month, kind: DocumentKind, slot: int = 0) -> bool`
    - `list_month(year, month) -> list[SharedDocumentRecord]`
    - `months_with_full_set() -> list[tuple[int, int]]` — months having ≥1 FA AND a packet, newest first

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/storage/test_shared_documents.py -v`
Expected: FAIL with `ModuleNotFoundError: nac_pay.storage.shared_documents`

- [ ] **Step 3: Implement**

Append to `src/nac_pay/storage/db_models.py`:

```python
class SharedDocumentRow(Base):
    """Admin-published monthly document shared by every account.

    The site admin uploads the company's Final Awards and Trip Pairing
    Packet once per month so pilots never handle PDFs. FINAL_AWARD is
    multi-slot (usually two files: FO + CA; occasionally one combined
    two-pager = one slot); TRIP_PACKET is slot 0 (re-upload replaces).
    ``uploaded_by`` is informational (no FK — an admin account deletion
    must not disturb published documents).

    New table — created by ``Base.metadata.create_all`` on first engine
    use, so no migration is needed on existing databases."""

    __tablename__ = "shared_documents"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[str] = mapped_column(String(40), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

Create `src/nac_pay/storage/shared_documents.py` (mirror the structure/idiom of `documents.py`):

```python
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

    def _month_dir(self, year: int, month: int) -> Path:
        return self._root / f"{year}-{month:02}"

    def _path_for(self, year: int, month: int, kind: DocumentKind, slot: int) -> Path:
        name = f"final_award_{slot}.pdf" if kind is DocumentKind.FINAL_AWARD else "packet.pdf"
        return self._month_dir(year, month) / name

    @staticmethod
    def _check_kind(kind: DocumentKind) -> None:
        if kind not in _SHARED_KINDS:
            raise ValueError(f"{kind.value} cannot be shared — FA and Packet only.")
    ...
```

Implement the remaining methods following `UserDocumentsStore` patterns exactly (lazy `from .db import session_scope` / `from .db_models import SharedDocumentRow` inside each method; `uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")`):

- `save_final_award`: `_check_kind` not needed (kind fixed); query used slots (like `save_stub`), `slot = max+1 or 0`, write file, insert row with `size_bytes=len(data)`.
- `save_packet`: like `UserDocumentsStore.save` — write file at slot 0, upsert row (update `original_filename`, `uploaded_at`, `uploaded_by`, `size_bytes` when the row exists).
- `list_final_awards` / `get_packet` / `list_month`: `select(SharedDocumentRow)` filtered, `order_by(SharedDocumentRow.slot)`; convert with a `_row_to_record` helper.
- `delete`: `_check_kind(kind)`; unlink file if exists; `delete(SharedDocumentRow).where(...)`; return `rowcount > 0`.
- `months_with_full_set`: select distinct `(year, month, kind)`, keep months where both `FINAL_AWARD` and `TRIP_PACKET` appear, `sorted(..., reverse=True)`.

Export in `src/nac_pay/storage/__init__.py` next to the documents import block:

```python
from .shared_documents import (  # noqa: E402
    SharedDocumentRecord,
    SharedDocumentsStore,
)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/storage/test_shared_documents.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/storage/db_models.py src/nac_pay/storage/shared_documents.py src/nac_pay/storage/__init__.py tests/storage/test_shared_documents.py
git commit -m "feat(storage): shared_documents table + SharedDocumentsStore (multi-slot FA, slot-0 packet)"
```

---

### Task 2: Central resolution — `documents_for_user` fallback, FA merge, `available_months`, feed updater

**Files:**
- Modify: `src/nac_pay/app/services.py:205-241` (`available_months`, `documents_for_user`) and `services.py:328-353` (`_pipeline` head)
- Modify: `src/nac_pay/app/feed_updater.py:136-142` (`_month_is_set_up`)
- Test: `tests/app/test_central_documents.py` (new)

**Interfaces:**
- Consumes: `SharedDocumentsStore`, `SharedDocumentRecord` (Task 1).
- Produces:
  - `documents_for_user(user_id, year, month) -> tuple[tuple[Path, ...], Path, Path | None] | None` — **FA is now a tuple of paths** (breaking change; this task fixes every consumer).
  - `_pipeline` merges FA dicts: later slot wins duplicate pilot codes.
  - `available_months(user_id)` = union(personal months, `months_with_full_set()`), newest first, same `(year, month, label)` tuples.
  - `feed_updater._month_is_set_up(store, year, month, user_id)` becomes central-aware (signature gains `user_id`? NO — replace body to call `documents_for_user`; change signature to `_month_is_set_up(user_id: str, year: int, month: int) -> bool` and update its two call sites in `update_user_feed`).

- [ ] **Step 1: Write the failing tests**

`tests/app/test_central_documents.py`. Use the real bundled fixture PDFs the existing tests use — look at `tests/app/test_onboarding.py::_docs_dir()` (repo `docs/` folder) and reuse whichever FA/Packet fixture files existing app tests upload (grep `test_documents.py` for the fixture path pattern and copy it; the May 2026 pair is the bundled sample). Test skeleton:

```python
"""Central (shared) document resolution: personal wins, shared is fallback."""

from __future__ import annotations

from pathlib import Path

from nac_pay.app.services import available_months, documents_for_user, invalidate_caches
from nac_pay.storage import (
    DocumentKind, SharedDocumentsStore, UserDocumentsStore, get_data_dir,
)

UID = "u_test_pilot"


def _fixture(name: str) -> bytes:
    # Same bundled sample documents the existing app tests use.
    return (Path(__file__).resolve().parents[2] / "docs" / name).read_bytes()


def _publish_shared(year=2026, month=5):
    s = SharedDocumentsStore(get_data_dir())
    s.save_final_award(year, month, "fa-shared.pdf", _fixture(<FA fixture name>), uploaded_by="admin")
    s.save_packet(year, month, "packet-shared.pdf", _fixture(<packet fixture name>), uploaded_by="admin")
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
    store.save(2026, 5, DocumentKind.FINAL_AWARD, "mine.pdf", _fixture(<FA fixture name>))
    fa_paths, _, _ = documents_for_user(UID, 2026, 5)
    assert len(fa_paths) == 1 and f"users/{UID}" in str(fa_paths[0])


def test_no_docs_returns_none():
    assert documents_for_user(UID, 2031, 1) is None


def test_available_months_unions_personal_and_shared():
    _publish_shared(2026, 5)
    store = UserDocumentsStore(get_data_dir(), UID)
    store.save(2026, 6, DocumentKind.FINAL_AWARD, "fa.pdf", b"%PDF", )
    months = [(y, m) for (y, m, _) in available_months(UID)]
    assert (2026, 5) in months and (2026, 6) in months
    assert months == sorted(months, reverse=True)


def test_two_shared_fa_files_merge_pilot_codes():
    # Publish the same FA PDF twice as two slots: the merged grid must
    # contain the fixture's pilot codes (dict-update, no crash), and the
    # pipeline must find the default profile's pilot.
    ...  # exercise via nac_pay.app.services._pipeline or load_dashboard for a
         # user whose profile pilot_id is a code in the fixture FA
```

For the merge test: create the user's profile with `PilotProfileStore(get_data_dir(), UID).save(...)` using a pilot code present in the fixture FA (grep an existing app test, e.g. `test_dashboard.py`, for the profile it builds — reuse that helper/values), then call `load_dashboard(2026, 5, UID)` and assert it returns data.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/app/test_central_documents.py -v`
Expected: FAIL (`documents_for_user` returns None for shared-only months; tuple shape mismatch)

- [ ] **Step 3: Implement**

In `services.py` — `documents_for_user`:

```python
def documents_for_user(
    user_id: str, year: int, month: int,
) -> tuple[tuple[Path, ...], Path, Path | None] | None:
    """Resolve ((final_award, ...), packet, ical_or_None) for a (user, month).

    Personal uploads always win; the admin-published shared documents are
    the fallback (FA may be several files — FO sheet + CA sheet — merged
    downstream by pilot code). Returns None when neither source has both
    an FA and a packet. The iCal feed is personal-only."""
    if user_id == DEFAULT_USER_ID and (year, month) in _DOC_INDEX:
        fa, packet, ical = _DOC_INDEX[(year, month)]
        return ((fa,), packet, ical)

    store = UserDocumentsStore(get_data_dir(), user_id)
    shared = SharedDocumentsStore(get_data_dir())

    own_fa = store.get(year, month, DocumentKind.FINAL_AWARD)
    if own_fa is not None:
        fa_paths: tuple[Path, ...] = (own_fa.path,)
    else:
        fa_paths = tuple(r.path for r in shared.list_final_awards(year, month))

    own_packet = store.get(year, month, DocumentKind.TRIP_PACKET)
    packet_path = own_packet.path if own_packet is not None else None
    if packet_path is None:
        shared_packet = shared.get_packet(year, month)
        packet_path = shared_packet.path if shared_packet is not None else None

    ical = store.get(year, month, DocumentKind.ICAL_FEED)
    if not fa_paths or packet_path is None:
        return None
    return (fa_paths, packet_path, ical.path if ical is not None else None)
```

Add `SharedDocumentsStore` to the `nac_pay.storage` import block at the top of `services.py`.

`_pipeline` head (`services.py:333-353`) becomes:

```python
    paths = documents_for_user(user_id, year, month)
    if paths is None:
        raise ValueError(...)   # unchanged message (Task 8 retypes it)
    fa_paths, packet_path, feed_path = paths
    ...
    fa_grids: dict[str, object] = {}
    for p in fa_paths:                      # slot order; later slot wins a dup code
        fa_grids.update(_parse_master_schedule(str(p)))
    sched = fa_grids.get(pilot_code)
    if sched is None:
        fa_names = ", ".join(p.name for p in fa_paths)
        raise ValueError(
            f"Pilot {pilot_code} not found in {fa_names}. "
            f"Available: {sorted(fa_grids)}"
        )
```

`available_months`:

```python
    if uid == DEFAULT_USER_ID:
        months = sorted(_DOC_INDEX.keys(), reverse=True)
    else:
        personal = UserDocumentsStore(get_data_dir(), uid).available_months()
        shared = SharedDocumentsStore(get_data_dir()).months_with_full_set()
        months = sorted(set(personal) | set(shared), reverse=True)
```

`feed_updater.py`: replace `_month_is_set_up(store, year, month)` with

```python
def _month_is_set_up(user_id: str, year: int, month: int) -> bool:
    """True when the month is computable for this user — FA + Packet exist
    from EITHER the user's own uploads or the admin-published shared set.
    Central docs mean a fresh pilot's months are set up on day one."""
    from .services import documents_for_user
    return documents_for_user(user_id, year, month) is not None
```

and update the call in `update_user_feed` to `_month_is_set_up(user_id, y, m)`. Check `tests/app/test_feed_updater.py` for tests that monkeypatch or call `_month_is_set_up` and update them to the new signature.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/app/test_central_documents.py tests/app/test_feed_updater.py tests/app/test_dashboard.py -v`
Expected: all PASS (dashboard test guards the default-user `_DOC_INDEX` path)

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/services.py src/nac_pay/app/feed_updater.py tests/app/test_central_documents.py tests/app/test_feed_updater.py
git commit -m "feat(services): resolve shared documents personal-first; multi-file FA merge; central-aware feed updater"
```

---

### Task 3: Admin gate + `/admin/documents`

**Files:**
- Create: `src/nac_pay/auth/admin.py`; export `is_admin` from `src/nac_pay/auth/__init__.py`
- Create: `src/nac_pay/app/admin_routes.py`, `src/nac_pay/app/templates/admin_documents.html`
- Modify: `src/nac_pay/app/main.py:133-137` (include router), `src/nac_pay/app/templates/base.html:24-32` (nav), `src/nac_pay/app/static_version.py` (register a `nav_is_admin` template global — it is the one registration hook every template env already calls)
- Test: `tests/app/test_admin_documents.py` (new)

**Interfaces:**
- Consumes: `SharedDocumentsStore` (Task 1), `_parse_master_schedule` / `_parse_trip_pairing_packet` (existing, `services.py:317-324`), `UserStore.get(user_id).email`.
- Produces:
  - `nac_pay.auth.is_admin(user_id: str) -> bool` — True when auth is off (dev convenience) or the user's email is in `ADMIN_EMAILS` (comma-separated, case-insensitive).
  - Routes `GET /admin/documents`, `POST /admin/documents/upload`, `POST /admin/documents/delete`; non-admin → `HTTPException(404)`.
  - Template global `nav_is_admin(request) -> bool` used by `base.html`.

- [ ] **Step 1: Write the failing tests**

```python
"""Admin document publishing — env-var gate + upload/delete + parse-on-upload."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.auth import is_admin

# Reuse the signup helper pattern from tests/app/test_onboarding.py
# (_signup_and_verify + subscription promotion) — copy it into this file.


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
    fa_bytes = (Path(__file__).resolve().parents[2] / "docs" / <FA fixture name>).read_bytes()
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/app/test_admin_documents.py -v`
Expected: FAIL (`ImportError: cannot import name 'is_admin'`)

- [ ] **Step 3: Implement**

`src/nac_pay/auth/admin.py`:

```python
"""Admin designation — an env-var allowlist, not a role system.

``ADMIN_EMAILS`` (comma-separated, case-insensitive) names the accounts
allowed to publish shared documents. When auth is off (dev), the default
user is treated as admin so the pages stay testable locally."""

from __future__ import annotations

import os

from .dependencies import auth_required


def admin_emails() -> frozenset[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_admin(user_id: str) -> bool:
    if not auth_required():
        return True
    from nac_pay.storage import UserStore
    user = UserStore().get(user_id)
    if user is None or not user.email:
        return False
    return user.email.lower() in admin_emails()
```

Export `admin_emails, is_admin` from `auth/__init__.py` (imports + `__all__`).

`src/nac_pay/app/admin_routes.py` — follow `document_routes.py` structure (own `Jinja2Templates` + `_register_static_v`, `_user_id_for` helper). Gate:

```python
def _require_admin(request: Request) -> str:
    user_id = _user_id_for(request)
    if not is_admin(user_id):
        raise HTTPException(status_code=404)   # don't advertise the surface
    return user_id
```

- `GET /admin/documents` (`?ym=YYYY-M` optional, default `date.today()`): context = selected year/month, `store.list_month(...)` rows each enriched with parse feedback — for FA slots call `services._parse_master_schedule(str(path))` in a try/except and pass `pilot_codes: sorted(grids)` or the error string; for the packet `services._parse_trip_pairing_packet` and `trip_count: len(result)`. Parses are the cached wrappers — cheap after first call. Also pass `months = store.months_with_full_set()` for a published-months list, and `uploaded`/`deleted`/`error` query params like `documents_list` does.
- `POST /admin/documents/upload` (`year`, `month`, `kind` in {`FINAL_AWARD`, `TRIP_PACKET`}, `upload: UploadFile`): validate month 1-12, `.pdf` suffix, 25MB cap, non-empty (copy the guards from `documents_upload`, `document_routes.py:117-144`). Save via `save_final_award` / `save_packet` with `uploaded_by=user_id`. **Parse-on-upload:** after saving, `try: services._parse_master_schedule(str(rec.path))` (FA — also reject `len(grids) == 0` with "No pilot bands found") or `_parse_trip_pairing_packet` (packet); on failure `store.delete(year, month, kind_enum, rec.slot)` then redirect `?error=Could+not+parse+...`. On success `invalidate_caches()` and redirect `?ym={year}-{month}&uploaded={kind}`.
- `POST /admin/documents/delete` (`year`, `month`, `kind`, `slot`): `_require_admin`, `store.delete`, `invalidate_caches()`, redirect back.

`admin_documents.html`: extends `base.html`, `active_screen: "admin"`. A month picker form (GET, `ym` input like the switcher), a table of published files (kind, slot, original filename, uploaded_at, size, parse feedback — "N pilot codes: AAA, BBB…" / "N trips" / red error), per-row delete forms, and two upload forms (Final Award file → kind hidden `FINAL_AWARD`; Packet → `TRIP_PACKET`). Copy form/table classes from `documents.html` so it inherits existing styling.

Nav (`base.html:31`): after Settings add

```html
{% if nav_is_admin(request) %}<a href="/admin/documents" class="nav-link {% if active_screen == 'admin' %}nav-link--active{% endif %}">Admin</a>{% endif %}
```

`static_version.py` `register(templates)`: add

```python
    def _nav_is_admin(request) -> bool:
        from nac_pay.auth import auth_required, is_admin
        if not auth_required():
            return True
        uid = request.session.get("user_id") if hasattr(request, "session") else None
        return bool(uid) and is_admin(uid)

    templates.env.globals["nav_is_admin"] = _nav_is_admin
```

(Note in the module docstring that `register` is the shared template-env setup hook, not just the static version.) `main.py`: `from .admin_routes import router as admin_router` + `app.include_router(admin_router)` alongside the others.

**Check nav fallout:** with auth off, `nav_is_admin` is True, so EVERY existing page test now renders an extra "Admin" link. Grep `tests/app` for assertions on the nav (e.g. exact-count or "Settings</a>" adjacency) and fix any that break.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/app/test_admin_documents.py tests/app/test_dashboard.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/auth/admin.py src/nac_pay/auth/__init__.py src/nac_pay/app/admin_routes.py src/nac_pay/app/templates/admin_documents.html src/nac_pay/app/templates/base.html src/nac_pay/app/static_version.py src/nac_pay/app/main.py tests/app/test_admin_documents.py
git commit -m "feat(admin): ADMIN_EMAILS gate + /admin/documents publish page with parse-on-upload"
```

---

### Task 4: Document view routes + Documents-page shared section

**Files:**
- Modify: `src/nac_pay/app/document_routes.py` (add view routes; extend `documents_list` context)
- Modify: `src/nac_pay/app/templates/documents.html` (View links + "Provided by the site" section)
- Verify (read; modify only if needed): the service worker template served by `src/nac_pay/app/pwa.py` — `/documents/view/` responses must NOT be cached
- Test: `tests/app/test_document_view.py` (new)

**Interfaces:**
- Consumes: `documents_for_user` (Task 2), `SharedDocumentsStore` (Task 1).
- Produces: `GET /documents/view/{year}/{month}/{kind}` and `GET /documents/view/{year}/{month}/{kind}/{slot}` returning `FileResponse` (inline `application/pdf`), personal-first-then-shared; 404 when nothing resolves. kind path segment = `DocumentKind` value (`FINAL_AWARD` / `TRIP_PACKET`).

- [ ] **Step 1: Write the failing tests**

```python
def test_view_streams_shared_packet(monkeypatch):
    # publish shared packet (fixture bytes) as in test_central_documents,
    # signup a pilot, GET /documents/view/2026/5/TRIP_PACKET
    r = client.get("/documents/view/2026/5/TRIP_PACKET")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert "inline" in r.headers.get("content-disposition", "inline")

def test_view_personal_beats_shared(...):
    # user uploads own FA with distinctive bytes; view returns those bytes

def test_view_404_when_missing(...):
    assert client.get("/documents/view/2031/1/FINAL_AWARD").status_code == 404

def test_view_fa_slot_selects_file(...):
    # two shared FA slots with different bytes; /FINAL_AWARD/1 returns slot 1's

def test_documents_page_lists_shared_docs_with_view_links(...):
    # publish shared 2026-5; page shows "Provided by the site" and an
    # href="/documents/view/2026/5/TRIP_PACKET" link
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/app/test_document_view.py -v` → 404s/missing markup.

- [ ] **Step 3: Implement**

In `document_routes.py`:

```python
from fastapi.responses import FileResponse

_VIEWABLE = {DocumentKind.FINAL_AWARD, DocumentKind.TRIP_PACKET}


@router.get("/documents/view/{year}/{month}/{kind}")
@router.get("/documents/view/{year}/{month}/{kind}/{slot}")
def document_view(
    request: Request, year: int, month: int, kind: str, slot: int = 0,
) -> FileResponse:
    """Stream the FA/Packet PDF the pipeline would use for this month —
    personal upload first, shared fallback. Never cached by the service
    worker (multi-megabyte; see pwa.py)."""
    user_id = _user_id_for(request)
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        raise HTTPException(404)
    if kind_enum not in _VIEWABLE:
        raise HTTPException(404)

    from nac_pay.storage import SharedDocumentsStore
    store = UserDocumentsStore(get_data_dir(), user_id)
    own = store.get(year, month, kind_enum)
    if own is not None and own.exists:
        path, name = own.path, own.original_filename
    else:
        shared = SharedDocumentsStore(get_data_dir())
        if kind_enum is DocumentKind.FINAL_AWARD:
            recs = [r for r in shared.list_final_awards(year, month) if r.slot == slot]
            rec = recs[0] if recs else None
        else:
            rec = shared.get_packet(year, month)
        if rec is None or not rec.path.exists():
            raise HTTPException(404)
        path, name = rec.path, rec.original_filename
    return FileResponse(
        path, media_type="application/pdf", filename=name,
        content_disposition_type="inline",
    )
```

(Default-user note: `_DOC_INDEX` months live on disk too — resolving via `UserDocumentsStore` returns None for them; acceptable: the view links are rendered only for real rows. Do not special-case.)

`documents_list` context: add `shared_by_month` — for each `(y, m)` in `SharedDocumentsStore(get_data_dir()).months_with_full_set()`, `{"year": y, "month": m, "month_label": _month_label(y, m), "final_awards": [...records...], "packet": record}`. Template: a "Provided by the site" card above the personal uploads listing each month's FA slots + packet as `<a href="/documents/view/{{y}}/{{m}}/FINAL_AWARD/{{r.slot}}">View</a>` (packet: no slot segment), plus View links next to the user's own FA/Packet rows in the existing per-month table (`/documents/view/.../{kind}` — personal wins automatically).

**Service worker check:** read the SW source served by `pwa.py`. If its fetch handler caches successful page responses (network-first-with-cache-put), add an early bailout for `url.pathname.startsWith('/documents/view/')` (pass through to `fetch` without `cache.put`). If it only caches `/static/` + navigations listed in the offline manifest, no change — record which in the commit message.

- [ ] **Step 4: Run to verify pass** — `pytest tests/app/test_document_view.py tests/app/test_documents.py tests/app/test_pwa.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/document_routes.py src/nac_pay/app/templates/documents.html tests/app/test_document_view.py [src/nac_pay/app/pwa.py]
git commit -m "feat(documents): in-app PDF viewing (personal-first) + shared-docs section on Documents page"
```

---

### Task 5: Onboarding step 2 → "Connect your live schedule"

**Files:**
- Modify: `src/nac_pay/app/onboarding_routes.py:153-215` (replace documents step with feed step)
- Create: `src/nac_pay/app/templates/onboarding/feed.html`; Delete: `templates/onboarding/documents.html`
- Modify: `src/nac_pay/app/onboarding_routes.py:150` (profile POST redirect target)
- Test: extend `tests/app/test_onboarding.py`

**Interfaces:**
- Consumes: `PilotProfileStore` / `PersistedPilotProfile` (existing), `feed_updater.update_user_feed` + `FeedFetchError` / `fetch_ical` (existing), central-aware `_month_is_set_up` (Task 2).
- Produces: `GET/POST /onboarding/feed`. Old `/onboarding/documents` routes are **removed**; `GET /onboarding/documents` returns a 303 redirect to `/onboarding/feed` (stale bookmarks/tabs).

- [ ] **Step 1: Write the failing tests** (append to `test_onboarding.py`)

```python
def test_onboarding_feed_saves_url_and_fetches(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    client = TestClient(app)
    uid = _signup_and_verify(client, "carol@example.com")
    calls = {}
    def fake_update(user_id, url, **kw):
        calls["args"] = (user_id, url)
        from nac_pay.app.feed_updater import UserUpdate
        return UserUpdate(user_id=user_id, months=())
    monkeypatch.setattr("nac_pay.app.onboarding_routes.update_user_feed", fake_update)
    r = client.post(
        "/onboarding/feed",
        data={"feed_url": "https://blueone.example/cal.ics"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/done"
    assert calls["args"] == (uid, "https://blueone.example/cal.ics")
    from nac_pay.app.services import load_persisted_profile
    p = load_persisted_profile(uid)
    assert p.feed_url == "https://blueone.example/cal.ics"
    assert p.feed_auto_update is True


def test_onboarding_feed_rejects_non_http(monkeypatch):
    ...  # POST feed_url="ftp://x" → 303 back to /onboarding/feed?error=...
         # and profile feed_url stays ""


def test_onboarding_feed_fetch_failure_rerenders(monkeypatch):
    ...  # fake_update raises FeedFetchError("boom") → redirect to
         # /onboarding/feed?error=... ; profile feed_url NOT saved


def test_onboarding_feed_empty_url_skips(monkeypatch):
    ...  # POST feed_url="" → 303 to /onboarding/done, profile untouched


def test_old_documents_step_redirects():
    ...  # GET /onboarding/documents → 303 /onboarding/feed
```

Also UPDATE the existing step-2 tests in this file that POST files to `/onboarding/documents` — they now describe removed behavior. Replace them with the tests above (keep any that assert the wizard's step gating, retargeted to `/onboarding/feed`).

- [ ] **Step 2: Run to verify failure** — `pytest tests/app/test_onboarding.py -v`

- [ ] **Step 3: Implement**

`onboarding_routes.py`: add `from .feed_updater import FeedFetchError, update_user_feed` (import at top, module is in same package — check for cycles: feed_updater imports from .services which doesn't import onboarding_routes → safe). Replace the two documents handlers with:

```python
@router.get("/onboarding/documents")
def onboarding_documents_redirect() -> RedirectResponse:
    """The old step-2 upload page — replaced by the feed-link step."""
    return RedirectResponse("/onboarding/feed", status_code=303)


@router.get("/onboarding/feed", response_class=HTMLResponse)
def onboarding_feed_get(request: Request, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "onboarding/feed.html",
        {"step": 2, "step_total": 3, "error": error, "active_screen": "onboarding"},
    )


@router.post("/onboarding/feed")
def onboarding_feed_post(
    request: Request, feed_url: str = Form(""),
) -> RedirectResponse:
    user_id = _user_id_for(request)
    if user_id is None or user_id == DEFAULT_USER_ID:
        return RedirectResponse("/onboarding/done", status_code=303)

    url = (feed_url or "").strip()
    if not url:
        return RedirectResponse("/onboarding/done", status_code=303)
    if not url.lower().startswith(("http://", "https://")):
        return RedirectResponse(
            "/onboarding/feed?error=The+feed+address+must+start+with+http",
            status_code=303,
        )

    # Fetch once right now (same path as the hourly updater) so the first
    # dashboard already has live data — and so a bad link fails here, where
    # the pilot can fix it, not silently an hour later.
    try:
        result = update_user_feed(user_id, url, today=date.today())
        if result.months and all(not m.ok for m in result.months):
            raise FeedFetchError(result.months[0].detail)
    except FeedFetchError as exc:
        return RedirectResponse(
            f"/onboarding/feed?error={quote_plus(f'Could not read that link: {exc}')}",
            status_code=303,
        )

    current = load_persisted_profile(user_id)
    PilotProfileStore(get_data_dir(), user_id).save(
        PersistedPilotProfile(
            profile=current.profile, feed_url=url, feed_auto_update=True,
        )
    )
    invalidate_caches()
    return RedirectResponse("/onboarding/done", status_code=303)
```

(`from urllib.parse import quote_plus`. Note `update_user_feed` itself catches `FeedFetchError` and returns failed `MonthUpdate`s — hence the all-months-failed check. Save the profile only AFTER a successful fetch, matching the tests.)

`templates/onboarding/feed.html` (modeled on the old documents.html):

```html
{% extends "onboarding/_step.html" %}
{% block title %}Connect your schedule — Setup — NAC Pay Tracker{% endblock %}

{% block content %}
<div class="auth-card">
  <h1>Connect your live schedule (optional)</h1>
  <p class="subtle">
    Paste your BlueOne calendar feed link and the app keeps your
    day-to-day flights current automatically — reroutes, drops,
    cancellations and pickups show up on their own.
  </p>

  {% if error %}
  <div class="banner banner--warn"><strong>{{ error }}</strong></div>
  {% endif %}

  <form action="/onboarding/feed" method="post" class="auth-form">
    <label>
      <span class="form-label">iCal feed link</span>
      <input type="url" name="feed_url" inputmode="url" autocomplete="off"
             placeholder="https://…" >
      <span class="form-hint">
        In BlueOne: export → calendar feed → <strong>copy the link</strong>
        (don't download the file).
      </span>
    </label>

    <button type="submit" class="btn btn--primary">Connect → Done</button>
  </form>

  <p class="subtle form-note">
    Your monthly schedule documents (Final Award and Trip Pairing Packet)
    are published to the site each month — you don't upload anything, and
    they don't update through this feed. Skipping this step just means
    mid-month changes won't appear automatically; you can paste the link
    later in Settings.
  </p>
  <form action="/onboarding/done" method="get">
    <button type="submit" class="btn">Skip for now</button>
  </form>
</div>
{% endblock %}
```

Update `onboarding_profile_post` (`:150`) redirect to `"/onboarding/feed"`. `git rm` the old template. Grep the repo for other `"/onboarding/documents"` references (middleware, templates, docs) and update.

- [ ] **Step 4: Run to verify pass** — `pytest tests/app/test_onboarding.py -v`

- [ ] **Step 5: Commit**

```bash
git add -A src/nac_pay/app/onboarding_routes.py src/nac_pay/app/templates/onboarding/ tests/app/test_onboarding.py
git commit -m "feat(onboarding): step 2 is a feed link (immediate fetch), not file uploads"
```

---

### Task 6: Pilot-code assist on step 1

**Files:**
- Modify: `src/nac_pay/app/services.py` (add `shared_pilot_directory`)
- Modify: `src/nac_pay/app/onboarding_routes.py` (lookup endpoint + POST-side non-JS check)
- Modify: `src/nac_pay/app/templates/onboarding/profile.html` (hint UI + vanilla JS)
- Test: `tests/app/test_pilot_code_assist.py` (new)

**Interfaces:**
- Consumes: `SharedDocumentsStore.months_with_full_set` / `list_final_awards` (Task 1), `_parse_master_schedule` (existing), `PilotMonthSchedule.last_name` (existing, `parsers/master_schedule.py:84-86`).
- Produces:
  - `services.shared_pilot_directory() -> tuple[str, dict[str, str]]` — `(month_label, {code: last_name})` from the current month's shared FA set, else the most recent earlier month with one; `("", {})` when none exists. Uses `date.today()` internally.
  - `GET /onboarding/code-lookup?code=XYZ` or `?last_name=smi` → JSON `{"month_label": "...", "matches": [{"code": "...", "last_name": "..."}]}` (exact-match on code after uppercase; case-insensitive prefix match on last name; max 10 matches).
  - Step-1 POST double-submit confirm: hidden field `confirmed_code`.

- [ ] **Step 1: Write the failing tests**

```python
def test_shared_pilot_directory_prefers_current_then_falls_back(...):
    # publish shared FA fixture for 2026-05 only; freeze "today" by
    # monkeypatching nac_pay.app.services.date (or pass today param —
    # implement shared_pilot_directory(today: date | None = None) for
    # testability and call it with date(2026, 7, 1))
    label, directory = shared_pilot_directory(today=date(2026, 7, 1))
    assert "May 2026" in label
    assert all(len(code) <= 4 for code in directory)
    assert directory   # fixture FA has pilot bands

def test_code_lookup_endpoint_by_code_and_lastname(...):
    # signed-up user mid-onboarding; GET /onboarding/code-lookup?code=<known>
    # → 1 match; ?last_name=<known prefix, lowercase> → ≥1 match;
    # ?code=ZZZ → 0 matches, still 200

def test_code_lookup_empty_when_no_shared_fa(...):
    # no shared docs → {"month_label": "", "matches": []}

def test_profile_post_warns_once_then_accepts(...):
    # directory has codes; POST profile with code "ZZZ" (absent) →
    # re-render (200) containing "not found" and hidden confirmed_code=ZZZ;
    # second POST including confirmed_code=ZZZ → 303 to /onboarding/feed

def test_profile_post_known_code_passes_straight_through(...):
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/app/test_pilot_code_assist.py -v`

- [ ] **Step 3: Implement**

`services.py`:

```python
def shared_pilot_directory(today: date_t | None = None) -> tuple[str, dict[str, str]]:
    """(month_label, {pilot_code: last_name}) from the shared Final Award —
    the current month's when published, else the most recent earlier month
    (codes are stable month to month). ("", {}) when nothing is published.
    Backs the onboarding pilot-code assist."""
    today = today or date_t.today()
    shared = SharedDocumentsStore(get_data_dir())
    months = shared.months_with_full_set()           # newest first
    if not months:
        return ("", {})
    past = [(y, m) for (y, m) in months if (y, m) <= (today.year, today.month)]
    year, month = past[0] if past else months[-1]
    directory: dict[str, str] = {}
    for rec in shared.list_final_awards(year, month):
        try:
            grids = _parse_master_schedule(str(rec.path))
        except Exception:                            # a bad slot never breaks signup
            continue
        directory.update({code: s.last_name for code, s in grids.items()})
    return (f"{_MONTH_NAMES[month]} {year}", directory)
```

(Check the `date` import name at the top of `services.py` — it imports `date as date_t`; match it.)

`onboarding_routes.py`:

```python
from fastapi.responses import JSONResponse
from .services import shared_pilot_directory


@router.get("/onboarding/code-lookup")
def onboarding_code_lookup(
    code: str = "", last_name: str = "",
) -> JSONResponse:
    label, directory = shared_pilot_directory()
    matches: list[dict[str, str]] = []
    if code.strip():
        c = code.strip().upper()
        if c in directory:
            matches = [{"code": c, "last_name": directory[c]}]
    elif last_name.strip():
        q = last_name.strip().lower()
        matches = [
            {"code": c, "last_name": ln}
            for c, ln in sorted(directory.items())
            if ln.lower().startswith(q)
        ][:10]
    return JSONResponse({"month_label": label, "matches": matches})
```

Step-1 POST (`onboarding_profile_post`): after `pilot_id_clean` validation, add the non-JS warn-once gate — change the handler signature to accept `confirmed_code: str = Form("")`, and instead of the redirect-on-warn, re-render the template directly (mirrors the GET context plus `warn`/`confirmed_code`):

```python
    label, directory = shared_pilot_directory()
    if (
        directory
        and pilot_id_clean not in directory
        and confirmed_code != pilot_id_clean
    ):
        return _TEMPLATES.TemplateResponse(
            request, "onboarding/profile.html",
            {
                "step": 1, "step_total": 3, "error": "",
                "persisted": load_persisted_profile(user_id),
                "active_screen": "onboarding",
                "warn": (
                    f"Code {pilot_id_clean} was not found on the "
                    f"{label} Final Award — double-check the code printed "
                    "on your award sheet, or submit again to continue anyway."
                ),
                "confirmed_code": pilot_id_clean,
                "form": {"name": name, "pilot_id": pilot_id_clean,
                         "position": position, "hourly_rate": hourly_rate},
            },
        )
```

(Return type of the handler widens to `HTMLResponse | RedirectResponse`.)

`profile.html`: under the pilot-code input add a hint area + find-my-code helper + hidden `confirmed_code` (renders `{{ confirmed_code or "" }}`), a warn banner when `warn` is set, re-fill fields from `form` when present, and this script (adjust the input's `id`/`name` to what the template actually uses — read it first):

```html
<div id="code-hint" class="form-hint" aria-live="polite"></div>
<details>
  <summary class="subtle">Find my code</summary>
  <label>
    <span class="form-label">Last name</span>
    <input type="text" id="lastname-lookup" autocomplete="off">
  </label>
  <div id="lookup-results" class="form-hint"></div>
</details>
<input type="hidden" name="confirmed_code" value="{{ confirmed_code or '' }}">

<script>
(function () {
  var codeInput = document.querySelector('input[name="pilot_id"]');
  var hint = document.getElementById('code-hint');
  var ln = document.getElementById('lastname-lookup');
  var out = document.getElementById('lookup-results');
  function check() {
    var v = (codeInput.value || '').trim();
    if (v.length < 2) { hint.textContent = ''; return; }
    fetch('/onboarding/code-lookup?code=' + encodeURIComponent(v))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.month_label) { hint.textContent = ''; return; }
        hint.textContent = d.matches.length
          ? '✓ ' + d.matches[0].code + ' — matches ' + d.matches[0].last_name +
            ' on the ' + d.month_label + ' Final Award'
          : 'Not found on the ' + d.month_label + ' Final Award — double-check ' +
            'the code printed on your award sheet.';
      })
      .catch(function () { hint.textContent = ''; });
  }
  if (codeInput) {
    codeInput.addEventListener('blur', check);
    codeInput.addEventListener('input', function () {
      clearTimeout(codeInput._t); codeInput._t = setTimeout(check, 400);
    });
  }
  if (ln) ln.addEventListener('input', function () {
    var v = (ln.value || '').trim();
    if (v.length < 2) { out.textContent = ''; return; }
    fetch('/onboarding/code-lookup?last_name=' + encodeURIComponent(v))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        out.textContent = d.matches.length
          ? d.matches.map(function (m) { return m.code + ' — ' + m.last_name; }).join(', ')
          : 'No match on the ' + (d.month_label || 'current') + ' Final Award.';
      })
      .catch(function () { out.textContent = ''; });
  });
})();
</script>
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/app/test_pilot_code_assist.py tests/app/test_onboarding.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/services.py src/nac_pay/app/onboarding_routes.py src/nac_pay/app/templates/onboarding/profile.html tests/app/test_pilot_code_assist.py
git commit -m "feat(onboarding): pilot-code assist — live check + find-my-code from the shared Final Award"
```

---

### Task 7: Delete account

**Files:**
- Create: `src/nac_pay/storage/account_delete.py`; export `delete_account` from `storage/__init__.py`
- Create: `src/nac_pay/app/account_routes.py`, `templates/account_delete.html`, `templates/account_deleted.html`
- Modify: `src/nac_pay/app/main.py` (include router), `templates/settings.html` (Danger Zone card before `form-actions`)
- Test: `tests/storage/test_account_delete.py` + `tests/app/test_account_delete_routes.py` (new)

**Interfaces:**
- Consumes: all ten user-keyed ORM models (`db_models.py`), `user_dir` (`storage/users.py:37`), `verify_password` + `UserStore` (existing), `clear_session` (`nac_pay.auth`).
- Produces: `delete_account(user_id: str) -> None` — raises `ValueError` for the default user; removes every DB row + the user's disk tree. Routes `GET /account/delete`, `POST /account/delete`.

- [ ] **Step 1: Write the failing tests**

`tests/storage/test_account_delete.py` — seed EVERY table, delete, assert emptiness and neighbor isolation:

```python
"""delete_account removes every trace of a user — DB rows and disk files."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from nac_pay.storage import delete_account, get_data_dir
from nac_pay.storage.db import session_scope
from nac_pay.storage import db_models as m
from nac_pay.storage.users import user_dir

ALL_USER_TABLES = [
    m.PilotProfileRow, m.DayOverrideRow, m.EmailVerificationRow,
    m.PasswordResetRow, m.UserDocumentRow, m.UserAssignmentVersionRow,
    m.UserVersionLegRow, m.FeedReassignmentDecisionRow, m.FeedDropDecisionRow,
]


def _seed(uid: str):
    with session_scope() as sess:
        sess.add(m.UserRow(user_id=uid, email=f"{uid}@x.com"))
        sess.add(m.PilotProfileRow(user_id=uid, pilot_id="AAA", name="A",
                                   position="FO", hourly_rate=100))
        sess.add(m.DayOverrideRow(user_id=uid, date_iso="2026-07-01"))
        sess.add(m.EmailVerificationRow(token=f"t-{uid}", user_id=uid,
                                        expires_at="2027-01-01T00:00:00"))
        sess.add(m.PasswordResetRow(token=f"p-{uid}", user_id=uid,
                                    expires_at="2027-01-01T00:00:00"))
        sess.add(m.UserDocumentRow(user_id=uid, year=2026, month=7,
                                   kind="FINAL_AWARD", slot=0,
                                   original_filename="fa.pdf",
                                   uploaded_at="2026-07-01T00:00:00"))
        sess.add(m.UserAssignmentVersionRow(
            user_id=uid, date_iso="2026-07-01", seq=1,
            version_type="REASSIGNMENT", pch_value=1,
            created_at="2026-07-01T00:00:00"))
        sess.add(m.UserVersionLegRow(user_id=uid, date_iso="2026-07-01",
                                     seq=1, idx=0))
        sess.add(m.FeedReassignmentDecisionRow(
            user_id=uid, date_iso="2026-07-01", signature="1/2",
            status="CONFIRMED", decided_at="2026-07-01T00:00:00"))
        sess.add(m.FeedDropDecisionRow(user_id=uid, date_iso="2026-07-02",
                                       status="REJECTED",
                                       decided_at="2026-07-01T00:00:00"))
    d = user_dir(get_data_dir(), uid) / "docs" / "2026-07"
    d.mkdir(parents=True, exist_ok=True)
    (d / "final_award.pdf").write_bytes(b"%PDF")


def _count(model, uid):
    with session_scope() as sess:
        return sess.execute(
            select(func.count()).select_from(model).where(model.user_id == uid)
        ).scalar_one()


def test_delete_account_removes_all_rows_and_files_and_spares_neighbors():
    _seed("u_gone"); _seed("u_stays")
    delete_account("u_gone")
    for model in ALL_USER_TABLES + [m.UserRow]:
        assert _count(model, "u_gone") == 0, model.__tablename__
        assert _count(model, "u_stays") == 1, model.__tablename__
    assert not user_dir(get_data_dir(), "u_gone").exists()
    assert user_dir(get_data_dir(), "u_stays").exists()


def test_delete_account_refuses_default_user():
    with pytest.raises(ValueError):
        delete_account("default")
```

`tests/app/test_account_delete_routes.py`:

```python
def test_delete_requires_correct_password_and_word(...):
    # signup+verify; POST /account/delete with wrong password → 200 page
    # containing "password" error, user still exists (find_by_email);
    # correct password but confirm="delete me" → error, still exists

def test_delete_happy_path_removes_user_and_ends_session(...):
    # POST password=<correct>, confirm="DELETE" → 200 containing
    # "Your account and all of its data have been deleted";
    # find_by_email(email) is None; a following GET / redirects to /login

def test_settings_page_shows_danger_zone(...):
    # GET /settings contains "Danger Zone" and a link to /account/delete
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/storage/test_account_delete.py -v`

- [ ] **Step 3: Implement**

`src/nac_pay/storage/account_delete.py`:

```python
"""Account deletion — immediate hard delete of a user's every trace.

One function so the future grace-period flow (deactivate now, purge on a
schedule once there are subscribers) can call the exact same removal.
Deletes are explicit per table rather than relying on FK cascade: four
tables have no ORM cascade relationship and SQLite FK enforcement is off
by default."""

from __future__ import annotations

import shutil

from sqlalchemy import delete as sa_delete

from .users import DEFAULT_USER_ID, user_dir


def delete_account(user_id: str) -> None:
    if user_id == DEFAULT_USER_ID:
        raise ValueError("The default (dev) account cannot be deleted.")
    from . import get_data_dir
    from .db import session_scope
    from . import db_models as m

    ordered = [
        m.UserVersionLegRow,
        m.UserAssignmentVersionRow,
        m.FeedReassignmentDecisionRow,
        m.FeedDropDecisionRow,
        m.UserDocumentRow,
        m.DayOverrideRow,
        m.EmailVerificationRow,
        m.PasswordResetRow,
        m.PilotProfileRow,
        m.UserRow,
    ]
    with session_scope() as sess:
        for model in ordered:
            sess.execute(sa_delete(model).where(model.user_id == user_id))

    shutil.rmtree(user_dir(get_data_dir(), user_id), ignore_errors=True)
```

Export from `storage/__init__.py`.

`src/nac_pay/app/account_routes.py` (own templates instance like the other routers; `_user_id_for` copied from `document_routes.py`):

```python
@router.get("/account/delete", response_class=HTMLResponse)
def account_delete_get(request: Request, error: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "account_delete.html",
        {"error": error, "active_screen": "settings"},
    )


@router.post("/account/delete", response_class=HTMLResponse)
def account_delete_post(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
) -> HTMLResponse:
    user_id = _user_id_for(request)
    if user_id == DEFAULT_USER_ID:
        return _error(request, "The demo account cannot be deleted.")
    user = UserStore().get(user_id)
    if user is None:
        return _error(request, "Account not found.")
    if confirm.strip() != "DELETE":
        return _error(request, 'Type DELETE (all capitals) to confirm.')
    if authenticate(user.email, password) is None:
        return _error(request, "That password is not correct.")

    delete_account(user_id)
    invalidate_caches()
    clear_session(request)
    return _TEMPLATES.TemplateResponse(
        request, "account_deleted.html", {"active_screen": ""},
    )
```

(`_error` = re-render `account_delete.html` with the message, status 200. `authenticate` is exported by `nac_pay.auth` — check its signature in `auth/users.py` and match; `clear_session` from `nac_pay.auth`. The goodbye page renders from THIS response, so no new public path is needed — the next request redirects to /login.)

`account_delete.html`: card titled "Delete your account" with the copy: "This permanently deletes your account, your uploaded documents, and every schedule record — immediately and unrecoverably." Password field, text field labeled `Type DELETE to confirm`, submit `btn--danger` (grep the CSS for the existing danger/warn button class; reuse what `documents.html` delete buttons use), cancel link back to `/settings`.
`account_deleted.html`: standalone card (extends base) — "Your account and all of its data have been deleted. Fly safe." with a link to `/signup`.

`settings.html`, before the `form-actions` div and OUTSIDE the main `<form>` (a nested form would break submission — the settings page is one big form; place the card after `</form>`):

```html
<div class="card">
  <h2 class="card-title">Danger Zone</h2>
  <p class="subtle">Deleting your account removes your profile, documents,
  and every schedule record — immediately and permanently.</p>
  <a class="btn" href="/account/delete">Delete account…</a>
</div>
```

`main.py`: include `account_router`.

- [ ] **Step 4: Run to verify pass** — `pytest tests/storage/test_account_delete.py tests/app/test_account_delete_routes.py tests/app/test_settings.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/storage/account_delete.py src/nac_pay/storage/__init__.py src/nac_pay/app/account_routes.py src/nac_pay/app/templates/account_delete.html src/nac_pay/app/templates/account_deleted.html src/nac_pay/app/templates/settings.html src/nac_pay/app/main.py tests/storage/test_account_delete.py tests/app/test_account_delete_routes.py
git commit -m "feat(account): immediate hard delete — Danger Zone, password + DELETE confirm, full DB+disk purge"
```

---

### Task 8: Friendly HTML empty states

**Files:**
- Modify: `src/nac_pay/app/services.py` (add `MonthDataError`; raise it at `services.py:334-338` and `:349-353`)
- Modify: `src/nac_pay/app/main.py` — the five `except ValueError` sites (`:781-782, :812-813, :845-846, :882-883, :915-916`) + a `_render_month_missing` helper
- Create: `src/nac_pay/app/templates/month_missing.html`
- Test: `tests/app/test_empty_states.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `services.MonthDataError(ValueError)` with attributes `flavor: str` (`"no_documents"` | `"pilot_not_found"`), `year: int`, `month: int`, `pilot_code: str` (empty for no_documents). Subclassing `ValueError` keeps every OTHER existing `except ValueError` (dashboard `:190`, feed updater, tests) working unchanged.

- [ ] **Step 1: Write the failing tests**

```python
"""Missing-month and unknown-pilot pages render HTML, not raw JSON."""


def test_calendar_missing_month_renders_html(...):
    # signup+verify+mark_completed, NO documents anywhere
    r = client.get("/calendar?year=2026&month=7")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "haven't been published" in r.text
    assert "{" not in r.text[:1]          # not a JSON body


def test_all_five_routes_render_html(...):
    for path in ("/calendar", "/pay", "/compare", "/discrepancies",
                 "/day/2026-07-15"):
        r = client.get(path if "day" in path else f"{path}?year=2026&month=7")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/html"), path


def test_unknown_pilot_code_flavor(...):
    # publish shared docs for 2026-05 (fixture), set profile pilot_id="ZZZ"
    r = client.get("/calendar?year=2026&month=5")
    assert r.status_code == 404
    assert "ZZZ" in r.text and "check your pilot code in Settings" in r.text
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/app/test_empty_states.py -v` (fails: content-type is application/json)

- [ ] **Step 3: Implement**

`services.py` (near the top, after imports):

```python
class MonthDataError(ValueError):
    """A month can't be computed — either no documents exist for it or the
    pilot's code isn't in the Final Award. Subclasses ValueError so existing
    generic handlers (dashboard empty state, feed updater) still catch it;
    the five data routes catch THIS type to render a friendly page."""

    def __init__(self, flavor: str, year: int, month: int,
                 message: str, pilot_code: str = ""):
        super().__init__(message)
        self.flavor = flavor
        self.year = year
        self.month = month
        self.pilot_code = pilot_code
```

Replace the two raises in `_pipeline`:

```python
        raise MonthDataError(
            "no_documents", year, month,
            f"No documents for {_MONTH_NAMES[month]} {year}.",
        )
...
        raise MonthDataError(
            "pilot_not_found", year, month,
            f"Pilot {pilot_code} not found in {fa_names}. Available: {sorted(fa_grids)}",
            pilot_code=pilot_code,
        )
```

`main.py`: import `MonthDataError` from `.services`; add

```python
def _render_month_missing(
    request: Request, exc: "MonthDataError", active_screen: str,
) -> HTMLResponse:
    from .services import _MONTH_NAMES  # or duplicate the list locally
    return _TEMPLATES.TemplateResponse(
        request,
        "month_missing.html",
        {
            "flavor": exc.flavor,
            "year": exc.year,
            "month": exc.month,
            "month_label": f"{_MONTH_NAMES[exc.month]} {exc.year}",
            "pilot_code": exc.pilot_code,
            "active_screen": active_screen,
        },
        status_code=404,
    )
```

(Check how `_MONTH_NAMES` is importable — `document_routes.py:207` keeps its own copy; do the same if the services name is private-by-convention only. Either is fine; be consistent with what you find.)

Each of the five routes: catch `MonthDataError` FIRST and render, keep the existing `except ValueError` → `HTTPException(404)` as the fallback for any other ValueError:

```python
    try:
        data = load_calendar(target_year, target_month, uid)
    except MonthDataError as exc:
        return _render_month_missing(request, exc, "calendar")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
```

(`/day/{date_iso}`: `active_screen="calendar"`; the others use their own screen names as currently passed at `main.py:786, :817, :851, :921`.)

`month_missing.html` — extends `base.html`, mimic `dashboard_empty.html`'s structure (read it first and copy its context expectations, e.g. `current_ym`):

```html
{% extends "base.html" %}
{% block title %}{{ month_label }} — NAC Pay Tracker{% endblock %}
{% block content %}
<section class="card">
  {% if flavor == "pilot_not_found" %}
    <h1>We couldn't find pilot code {{ pilot_code }}</h1>
    <p>Code <strong>{{ pilot_code }}</strong> isn't on the {{ month_label }}
    Final Award — check your pilot code in
    <a href="/settings">Settings</a>. It's the 3-letter code printed on
    your award sheet.</p>
  {% else %}
    <h1>{{ month_label }} isn't ready yet</h1>
    <p>The {{ month_label }} schedule documents haven't been published to
    the site yet — check back soon, or upload your own copies via
    <a href="/documents">Documents</a>.</p>
  {% endif %}
</section>
{% endblock %}
```

**Check the dashboard too:** `main.py:188-202` catches bare `ValueError` for its own empty state — with `MonthDataError` subclassing `ValueError` it keeps working. Add one assertion to `test_empty_states.py` that `/` still renders `dashboard_empty` (status 200) for a docless user.

- [ ] **Step 4: Run to verify pass** — `pytest tests/app/test_empty_states.py tests/app/test_calendar.py tests/app/test_dashboard.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/nac_pay/app/services.py src/nac_pay/app/main.py src/nac_pay/app/templates/month_missing.html tests/app/test_empty_states.py
git commit -m "feat(app): HTML empty states for missing months and unknown pilot codes (no more raw JSON)"
```

---

### Task 9: Docs, full suite, PR

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md` — §10 (document acquisition: central/shared docs + per-user override; onboarding step 2 = feed link), §13 (screens table: Admin documents page, Danger Zone/delete, month_missing states), §14 config matrix (`ADMIN_EMAILS`), new changelog entry dated 2026-07-26 (also note that PR #59 closed the two cosmetic follow-ups the 2026-07-24 (2) entry left open)
- No code changes in this task.

- [ ] **Step 1: Update SYSTEM_CONTEXT.md** as above. Changelog entry should summarize: central shared documents (admin-published, personal override), `/admin/documents` + `ADMIN_EMAILS`, onboarding feed-link step + pilot-code assist, in-app PDF viewing, account deletion (hard delete now, grace period later), HTML empty states.

- [ ] **Step 2: Run the FULL suite in background**

```bash
# via Bash run_in_background:
python -m pytest -n auto > /private/tmp/.../scratchpad/full_suite.txt 2>&1; echo "RC=$?"
```

Then verify: the `RC=` line printed by the SAME shell (never a pipe), plus `grep -c "FAILED\|ERROR" full_suite.txt` (expect 0) and read the summary line. Fix any failures before proceeding.

- [ ] **Step 3: Commit docs**

```bash
git add docs/SYSTEM_CONTEXT.md
git commit -m "docs(changelog): onboarding overhaul — central docs, feed-link signup, account deletion, empty states"
```

- [ ] **Step 4: Push branch + open PR** (do NOT merge without the author's go-ahead)

```bash
git push -u origin feat/onboarding-central-docs
gh pr create --title "Onboarding overhaul: central admin-published documents, feed-link signup, account deletion, friendly empty states" --body "<summary per repo convention>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Deploy (after merge, separately): add `ADMIN_EMAILS=dennfish@gmail.com` to the box's `deploy/.env.prod`, pull main, rebuild, verify `/api/health` box-local and via Cloudflare, then the author uploads the August FA (FO + CA) + Packet at `/admin/documents`. The two August PDFs in the repo's untracked `docs/` folder are those inputs — upload through the app, never commit them.

---

## Plan Self-Review Notes

- Spec coverage: §1 → Tasks 1-4; §2 (feed step + code assist) → Tasks 5-6; §3 → Task 7; §4 → Task 8; error-handling table → Tasks 3/5/7/8; testing list → per-task tests; deploy notes → Task 9. Document viewing (spec §1 last block) → Task 4. No gaps found.
- Types cross-checked: `documents_for_user` new return shape is consumed in Tasks 2 (pipeline), 4 (view uses stores directly — intentional, slot-addressable), 5 (via `_month_is_set_up`). `SharedDocumentsStore` method names identical across Tasks 1-6. `MonthDataError` only in Task 8.
- Known judgment calls left to implementers: exact fixture filenames in `docs/` (grep existing tests), the SW caching check (Task 4), nav-assertion fallout (Task 3), `authenticate()` exact signature (Task 7).
