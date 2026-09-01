"""Admin / recovery dashboard (SRS 2.9).

The dashboard is a static client-rendered page (``app/static/``) behind the admin
credential; it polls ``/admin/status.json`` and posts to ``/admin/cookies``. The
credential is separate from the generation ``api_keys``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from .admin_auth import attach_admin_session, require_admin
from .cookie_admin import apply_cookie_payload
from .status_report import build_full_status

logger = logging.getLogger("gemini_proxy.admin")

router = APIRouter(tags=["admin"])

STATIC_DIR = Path(__file__).parent / "static"
_INDEX = STATIC_DIR / "index.html"


@router.get("/admin", dependencies=[Depends(require_admin)])
async def admin_page(request: Request) -> FileResponse:
    return attach_admin_session(
        FileResponse(_INDEX, media_type="text/html"), request
    )


@router.get("/admin/status.json", dependencies=[Depends(require_admin)])
async def admin_status(request: Request) -> dict[str, Any]:
    """Authenticated full status (superset of ``GET /status``) for the dashboard
    and for monitoring tools that want it gated."""
    return await build_full_status(request.app)


@router.post("/admin/cookies", dependencies=[Depends(require_admin)])
async def admin_apply_cookies(request: Request) -> JSONResponse:
    gemini = request.app.state.gemini
    cookie_store = request.app.state.cookie_store

    raw: str | None = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = body.get("cookies")
            elif isinstance(body, (list, dict)):
                raw = json.dumps(body)
        except Exception:  # noqa: BLE001
            raw = None
    else:
        form = await request.form()
        raw = form.get("cookies") if form else None
    if not raw:
        raw = (await request.body()).decode("utf-8", "ignore") or None

    if not raw or not raw.strip():
        return JSONResponse(status_code=400, content={"error": "no cookie payload provided"})

    try:
        result = await apply_cookie_payload(gemini, cookie_store, raw)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    code = 200 if result.get("reinit_ok", True) else 502
    return JSONResponse(status_code=code, content=result)
