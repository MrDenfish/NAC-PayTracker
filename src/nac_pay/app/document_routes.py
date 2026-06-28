"""User document upload routes — list + upload + delete.

Each (user, year, month, kind) slot holds one current document. Re-upload
replaces. Default-user has no upload UI (they use the bundled docs/
corpus); the route still loads for them so the page is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
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
            "active_screen": "documents",
            "single_kinds": [
                k for k in DocumentKind if k is not DocumentKind.PAY_STUB
            ],
            "uploaded": request.query_params.get("uploaded"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error", ""),
        },
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
            if existing is not None and existing.exists()
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
