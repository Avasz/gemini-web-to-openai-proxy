"""API-key auth for the generation endpoints (SRS 2.3).

Kept deliberately separate from the admin/status credential (SRS 2.9) -- a
generation key must not grant admin access. When ``api_keys`` is empty the
endpoints are open (a startup warning is logged elsewhere).

Accepted forms:
  * ``Authorization: Bearer <key>``  (OpenAI clients)
  * ``x-api-key: <key>``             (Google / Anthropic style clients)
  * ``?key=<key>``                   (Google GenAI client query param)
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status


def _present_key(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    goog = request.headers.get("x-goog-api-key")
    if goog:
        return goog.strip()
    key_param = request.query_params.get("key")
    if key_param:
        return key_param.strip()
    return None


async def require_api_key(request: Request) -> None:
    configured = list(getattr(request.app.state.config, "api_keys", []) or [])
    if not configured:
        return
    supplied = _present_key(request)
    if supplied is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (Authorization: Bearer, x-api-key, or ?key=).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if supplied not in configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
        )
