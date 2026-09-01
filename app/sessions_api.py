"""Warm-session lifecycle endpoints (SRS 2.11) -- opt-in.

  POST   /v1/sessions          start a session (sends one priming message)
  GET    /v1/sessions          list active sessions
  GET    /v1/sessions/{id}     inspect one
  DELETE /v1/sessions/{id}     close one

Generation requests opt in by passing ``session_id`` (OpenAI / Responses) or
``sessionId`` (Google) in the body; see the per-surface docs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import require_api_key
from .model_selection import ModelNotAvailable, resolve
from .warm_sessions import SessionError

logger = logging.getLogger("gemini_proxy.sessions")

router = APIRouter(tags=["warm-sessions"], dependencies=[Depends(require_api_key)])


def _mgr(request: Request):
    mgr = request.app.state.warm_sessions
    if mgr is None:
        raise HTTPException(status_code=404, detail="Warm sessions are not enabled.")
    return mgr


def resolve_session(request: Request, session_id: str | None):
    """For the generation endpoints: return the WarmSession for ``session_id`` or
    ``None`` if not requested. An unknown/expired id is a clean 409 -- never a
    silent fresh conversation (SRS 2.11)."""
    if not session_id:
        return None
    mgr = request.app.state.warm_sessions
    if mgr is None:
        raise HTTPException(
            status_code=400,
            detail="session_id was given but warm sessions are not enabled.",
        )
    try:
        sess = mgr.get(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return sess


@router.post("/v1/sessions")
async def start_session(request: Request) -> dict:
    mgr = _mgr(request)
    service = request.app.state.gemini
    cfg = request.app.state.config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    requested = body.get("model") or cfg.default_model
    priming = body.get("priming_message") or body.get("priming") or None

    try:
        client = await service.get_client()
        resolved = resolve(client, requested)
    except ModelNotAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Gemini unavailable: {exc}")

    if not resolved.model.is_available:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{requested}' is not available for this account.",
        )

    try:
        sess = await mgr.create(resolved, priming)
    except Exception as exc:  # noqa: BLE001
        logger.exception("warm session start failed")
        raise HTTPException(status_code=502, detail=f"Could not start session: {exc}")
    return sess.info()


@router.get("/v1/sessions")
async def list_sessions(request: Request) -> dict:
    return {"object": "list", "data": _mgr(request).list()}


@router.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    try:
        return _mgr(request).get(session_id).info()
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/v1/sessions/{session_id}")
async def close_session(session_id: str, request: Request) -> dict:
    closed = _mgr(request).close(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail="No such session.")
    return {"deleted": True, "session_id": session_id}
