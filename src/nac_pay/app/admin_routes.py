"""Admin document publishing — a site admin uploads each month's shared
Final Award + Trip Pairing Packet once; every account without a personal
upload resolves them via ``services.documents_for_user``.

Gated by ``ADMIN_EMAILS`` (see ``nac_pay.auth.is_admin``). Non-admin
requests get a plain 404 — the surface isn't advertised.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from nac_pay.auth import auth_required, is_admin
from nac_pay.storage import (
    DEFAULT_USER_ID,
    DocumentKind,
    SharedDocumentsStore,
    get_data_dir,
)

from . import services
from .static_version import register as _register_static_v

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_register_static_v(_TEMPLATES)

router = APIRouter()

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25MB — comfortable headroom over real PDFs


def _user_id_for(request: Request) -> str:
    if not auth_required():
        return DEFAULT_USER_ID
    return request.session.get("user_id") or DEFAULT_USER_ID


def _require_admin(request: Request) -> str:
    user_id = _user_id_for(request)
    if not is_admin(user_id):
        raise HTTPException(status_code=404)   # don't advertise the surface
    return user_id


def _parse_feedback(rec) -> dict:
    """Try the cached parser against a saved shared document and return a
    dict of feedback fields for the template. Never raises — parse errors
    become an ``error`` string so the row still renders."""
    path_str = str(rec.path)
    if rec.kind is DocumentKind.FINAL_AWARD:
        try:
            grids = services._parse_master_schedule(path_str)
        except Exception as exc:  # noqa: BLE001 — surfaced to the admin, not swallowed
            return {"error": str(exc)}
        return {"pilot_codes": sorted(grids)}
    else:
        try:
            result = services._parse_trip_pairing_packet(path_str)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {"trip_count": len(result)}


@router.get("/admin/documents", response_class=HTMLResponse)
def admin_documents_list(request: Request) -> HTMLResponse:
    _require_admin(request)

    ym = request.query_params.get("ym", "")
    if ym:
        try:
            y_str, m_str = ym.split("-", 1)
            year, month = int(y_str), int(m_str)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail=f"Invalid ym={ym!r}")
    else:
        today = date.today()
        year, month = today.year, today.month

    store = SharedDocumentsStore(get_data_dir())
    rows = []
    for rec in store.list_month(year, month):
        rows.append({"record": rec, "feedback": _parse_feedback(rec)})

    return _TEMPLATES.TemplateResponse(
        request,
        "admin_documents.html",
        {
            "year": year,
            "month": month,
            "ym": f"{year}-{month}",
            "rows": rows,
            "months": store.months_with_full_set(),
            "active_screen": "admin",
            "uploaded": request.query_params.get("uploaded"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/admin/documents/upload")
async def admin_documents_upload(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    kind: str = Form(...),
    upload: UploadFile = File(...),
) -> RedirectResponse:
    user_id = _require_admin(request)

    if not (1 <= month <= 12):
        return RedirectResponse(
            "/admin/documents?error=Invalid+month", status_code=303,
        )
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        return RedirectResponse(
            f"/admin/documents?error=Unknown+document+kind+{kind}", status_code=303,
        )
    if kind_enum not in (DocumentKind.FINAL_AWARD, DocumentKind.TRIP_PACKET):
        return RedirectResponse(
            "/admin/documents?error=Only+Final+Award+and+Packet+can+be+shared",
            status_code=303,
        )

    name = upload.filename or ""
    if not name.lower().endswith(".pdf"):
        return RedirectResponse(
            f"/admin/documents?ym={year}-{month}&error={kind_enum.value}+must+be+a+.pdf+file",
            status_code=303,
        )

    data = await upload.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return RedirectResponse(
            f"/admin/documents?ym={year}-{month}&error=File+too+large+%2825MB+max%29",
            status_code=303,
        )
    if not data:
        return RedirectResponse(
            f"/admin/documents?ym={year}-{month}&error=Empty+upload", status_code=303,
        )

    store = SharedDocumentsStore(get_data_dir())
    if kind_enum is DocumentKind.FINAL_AWARD:
        rec = store.save_final_award(year, month, name, data, uploaded_by=user_id)
    else:
        rec = store.save_packet(year, month, name, data, uploaded_by=user_id)

    # Parse-on-upload: reject a file that doesn't actually parse (garbage
    # PDF, wrong document entirely) instead of publishing something the
    # pipeline will choke on for every subscriber later.
    try:
        if kind_enum is DocumentKind.FINAL_AWARD:
            grids = services._parse_master_schedule(str(rec.path))
            if len(grids) == 0:
                raise ValueError("No pilot bands found")
        else:
            services._parse_trip_pairing_packet(str(rec.path))
    except Exception as exc:  # noqa: BLE001 — any parse failure rejects the upload
        store.delete(year, month, kind_enum, rec.slot)
        from urllib.parse import quote
        return RedirectResponse(
            f"/admin/documents?ym={year}-{month}&error="
            + quote(f"Could not parse {kind_enum.value}: {exc}"),
            status_code=303,
        )

    services.invalidate_caches()
    return RedirectResponse(
        f"/admin/documents?ym={year}-{month}&uploaded={kind_enum.value}",
        status_code=303,
    )


@router.post("/admin/documents/delete")
def admin_documents_delete(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    kind: str = Form(...),
    slot: int = Form(0),
) -> RedirectResponse:
    _require_admin(request)
    try:
        kind_enum = DocumentKind(kind)
    except ValueError:
        raise HTTPException(400, f"Unknown kind {kind!r}")
    if kind_enum not in (DocumentKind.FINAL_AWARD, DocumentKind.TRIP_PACKET):
        raise HTTPException(400, f"{kind_enum.value} cannot be shared")

    store = SharedDocumentsStore(get_data_dir())
    store.delete(year, month, kind_enum, slot)
    services.invalidate_caches()
    return RedirectResponse(
        f"/admin/documents?ym={year}-{month}&deleted={kind_enum.value}",
        status_code=303,
    )
