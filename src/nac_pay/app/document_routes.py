"""User document upload routes — list + upload + delete.

Each (user, year, month, kind) slot holds one current document. Re-upload
replaces. Default-user has no upload UI (they use the bundled docs/
corpus); the route still loads for them so the page is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
    SharedDocumentsStore,
    UserDocumentsStore,
    expected_extension,
    get_data_dir,
)

from .services import current_user
from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25MB — comfortable headroom over real PDFs

# Only these kinds are ever viewable in-browser — iCal feed and pay stubs
# aren't PDFs / aren't meant for inline display.
_VIEWABLE = {DocumentKind.FINAL_AWARD, DocumentKind.TRIP_PACKET}


def _user_id_for(request: Request) -> str:
    """current_user() depends on Request implicitly through middleware;
    we resolve it here for our routes (no Depends() since main.py routes
    use the same pattern without it)."""
    if not auth_required():
        return DEFAULT_USER_ID
    return request.session.get("user_id") or DEFAULT_USER_ID


@router.get("/documents", response_class=HTMLResponse)
def documents_list(request: Request) -> HTMLResponse:
    user_id = _user_id_for(request)
    is_default = user_id == DEFAULT_USER_ID
    store = UserDocumentsStore(get_data_dir(), user_id) if not is_default else None

    # slots[(year,month)][kind] = {filename, uploaded_at} for FA/Packet/iCal
    # stubs[(year,month)] = [ {slot, filename, uploaded_at}, ... ] for PAY_STUB
    slots: dict[tuple[int, int], dict[str, dict]] = {}
    stubs: dict[tuple[int, int], list[dict]] = {}
    if store is not None:
        for rec in store.list_all():
            key = (rec.year, rec.month)
            if rec.kind is DocumentKind.PAY_STUB:
                stubs.setdefault(key, []).append({
                    "slot": rec.slot,
                    "original_filename": rec.original_filename,
                    "uploaded_at": rec.uploaded_at,
                })
            else:
                slots.setdefault(key, {})[rec.kind.value] = {
                    "original_filename": rec.original_filename,
                    "uploaded_at": rec.uploaded_at,
                }
        for lst in stubs.values():
            lst.sort(key=lambda s: s["slot"])

    sorted_months = sorted(set(slots) | set(stubs), reverse=True)

    shared_store = SharedDocumentsStore(get_data_dir())
    shared_by_month = [
        {
            "year": y,
            "month": m,
            "month_label": _month_label(y, m),
            "final_awards": shared_store.list_final_awards(y, m),
            "packet": shared_store.get_packet(y, m),
        }
        for (y, m) in shared_store.months_with_full_set()
    ]

    return _TEMPLATES.TemplateResponse(
        request,
        "documents.html",
        {
            "is_default_user": is_default,
            "documents_by_month": [
                {
                    "year": y,
                    "month": m,
                    "month_label": _month_label(y, m),
                    "ym": f"{y}-{m}",
                    "slots": slots.get((y, m), {}),
                    "pay_stubs": stubs.get((y, m), []),
                }
                for (y, m) in sorted_months
            ],
            "shared_by_month": shared_by_month,
            "active_screen": "documents",
            "single_kinds": [
                k for k in DocumentKind if k is not DocumentKind.PAY_STUB
            ],
            "uploaded": request.query_params.get("uploaded"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/documents/view/{year}/{month}/{kind}")
@router.get("/documents/view/{year}/{month}/{kind}/{slot}")
def document_view(
    request: Request, year: int, month: int, kind: str, slot: int = 0,
) -> FileResponse:
    """Stream the FA/Packet PDF the pipeline would use for this month —
    personal upload first, shared fallback. Never cached by the service
    worker (multi-megabyte; see pwa.py — the fetch handler only caches
    text/html responses, so a PDF response is excluded automatically)."""
    user_id = _user_id_for(request)
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        raise HTTPException(404)
    if kind_enum not in _VIEWABLE:
        raise HTTPException(404)

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


@router.get("/documents/download/{year}/{month}/ical")
def document_download_ical(request: Request, year: int, month: int) -> FileResponse:
    """Download the stored (merge-preserved) iCal feed for a month.

    The stored ``feed.ics`` is the app's only archive of flown legs that
    have aged out of BlueOne's rolling window (see ``parsers.ical_merge``),
    so the pilot must be able to get their own copy back out — e.g. to
    re-examine a past day's actual times. Deliberately separate from
    ``/documents/view``: the feed is personal-only (never shared, no
    fallback), served as an attachment rather than inline, and stays out
    of ``_VIEWABLE`` so the PDF view route's semantics don't change.
    """
    user_id = _user_id_for(request)
    rec = UserDocumentsStore(get_data_dir(), user_id).get(
        year, month, DocumentKind.ICAL_FEED,
    )
    if rec is None or not rec.exists:
        raise HTTPException(404)
    return FileResponse(
        rec.path, media_type="text/calendar",
        filename=f"feed_{year}-{month:02d}.ics",
        content_disposition_type="attachment",
    )


@router.post("/documents/upload")
async def documents_upload(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    kind: str = Form(...),
    upload: UploadFile = File(...),
) -> RedirectResponse:
    user_id = _user_id_for(request)
    if user_id == DEFAULT_USER_ID:
        return RedirectResponse(
            "/documents?error=Default+user+cannot+upload+%E2%80%94+use+a+real+account",
            status_code=303,
        )
    if not (1 <= month <= 12):
        return RedirectResponse(
            "/documents?error=Invalid+month", status_code=303,
        )
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        return RedirectResponse(
            f"/documents?error=Unknown+document+kind+{kind}", status_code=303,
        )

    name = upload.filename or ""
    expected_ext = expected_extension(kind_enum)
    if not name.lower().endswith(expected_ext):
        return RedirectResponse(
            f"/documents?error={kind_enum.value}+must+be+a+{expected_ext}+file",
            status_code=303,
        )

    data = await upload.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return RedirectResponse(
            "/documents?error=File+too+large+%2825MB+max%29", status_code=303,
        )
    if not data:
        return RedirectResponse(
            "/documents?error=Empty+upload", status_code=303,
        )

    store = UserDocumentsStore(get_data_dir(), user_id)
    if kind_enum is DocumentKind.PAY_STUB:
        store.save_stub(year, month, name, data)
    elif kind_enum is DocumentKind.ICAL_FEED:
        # Merge-preserve so a re-upload of a fresh (rolling-window) feed can't
        # erase already-flown legs that have aged out of BlueOne. Same guard
        # the hourly updater uses — protects feeds when auto-update is off.
        from datetime import datetime, timezone

        from nac_pay.parsers import merge_feed_bytes
        existing = store.get(year, month, DocumentKind.ICAL_FEED)
        existing_bytes = (
            existing.path.read_bytes()
            if existing is not None and existing.exists
            else None
        )
        data = merge_feed_bytes(existing_bytes, data, datetime.now(timezone.utc))
        store.save(year, month, kind_enum, name, data)
    else:
        store.save(year, month, kind_enum, name, data)

    # Invalidate pipeline cache so the next render picks up the new doc.
    from .services import invalidate_caches
    invalidate_caches()
    return RedirectResponse(
        f"/documents?uploaded={year}-{month}-{kind_enum.value}",
        status_code=303,
    )


@router.post("/documents/delete")
def documents_delete(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    kind: str = Form(...),
    slot: int = Form(0),
) -> RedirectResponse:
    user_id = _user_id_for(request)
    if user_id == DEFAULT_USER_ID:
        return RedirectResponse("/documents", status_code=303)
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        raise HTTPException(400, f"Unknown kind {kind!r}")
    store = UserDocumentsStore(get_data_dir(), user_id)
    if kind_enum is DocumentKind.PAY_STUB:
        store.delete_stub(year, month, slot)
    else:
        store.delete(year, month, kind_enum)
    from .services import invalidate_caches
    invalidate_caches()
    return RedirectResponse(
        f"/documents?deleted={year}-{month}-{kind_enum.value}",
        status_code=303,
    )


# ── Helpers ──────────────────────────────────────────────────────────


_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_label(year: int, month: int) -> str:
    if 1 <= month <= 12:
        return f"{_MONTH_NAMES[month]} {year}"
    return f"{year}-{month}"
