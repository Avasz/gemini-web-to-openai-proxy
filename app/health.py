"""Three independent health signals (SRS 2.7).

They fail independently and mean different things, so they are reported
separately -- "the page loaded" must never stand in for "this is healthy":

  1. page_reachable       -- a bare GET to gemini.google.com/app works (the
                             cookie value itself isn't garbage)
  2. client_authenticated -- the library considers the session a real account
                             (account_status == AVAILABLE); can be false even
                             when #1 is true
  3. recent_requests_ok   -- generations have actually been completing over a
                             recent window (from the local request history)
"""

from __future__ import annotations

import logging
from typing import Any

from .activity_log import ActivityLog
from .gemini_service import GeminiService

logger = logging.getLogger("gemini_proxy.health")

_GEMINI_APP_URL = "https://gemini.google.com/app"
_ACCOUNT_AVAILABLE = 1000  # gemini_webapi.constants.AccountStatus.AVAILABLE
_RECENT_WINDOW_HOURS = 1.0


async def _page_reachable(service: GeminiService) -> bool | None:
    client = service._client  # noqa: SLF001
    session = getattr(client, "client", None) if client else None
    if session is None:
        return None
    try:
        resp = await session.get(_GEMINI_APP_URL)
        return 200 <= resp.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.debug("page reachability check failed: %s", exc)
        return False


def _account_status(service: GeminiService) -> tuple[bool, str | None]:
    client = service._client  # noqa: SLF001
    if client is None:
        return False, None
    raw = getattr(client, "account_status", None)
    code = int(raw) if raw is not None else None
    name = None
    try:
        from gemini_webapi.constants import AccountStatus

        name = AccountStatus(code).name if code is not None else None
    except Exception:  # noqa: BLE001
        name = str(code) if code is not None else None
    return code == _ACCOUNT_AVAILABLE, name


async def build_health(
    service: GeminiService, activity: ActivityLog | None
) -> dict[str, Any]:
    page = await _page_reachable(service)
    authed, status_name = _account_status(service)

    recent_ok: bool | None = None
    recent: dict[str, Any] = {}
    if activity is not None:
        recent = await activity.summary(_RECENT_WINDOW_HOURS)
        if recent.get("total"):
            recent_ok = recent["error_rate"] < 0.5

    if not service.is_ready() or page is False:
        overall = "down" if not authed else "degraded"
    elif not authed or recent_ok is False:
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "overall": overall,
        "page_reachable": page,
        "client_authenticated": authed,
        "account_status": status_name,
        "recent_requests_ok": recent_ok,
        "recent_window_hours": _RECENT_WINDOW_HOURS,
        "recent": {
            k: recent.get(k)
            for k in ("total", "ok", "errors", "error_rate")
            if k in recent
        },
    }
