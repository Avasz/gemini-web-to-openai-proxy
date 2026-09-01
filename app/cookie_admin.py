"""Apply a pasted / watched cookie export at runtime (SRS 2.9).

Writes the new cookies to the configured ``cookie_file`` and tears down + rebuilds
the shared Gemini client so the new credentials take effect with no restart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .cookies import CookieStore, parse_cookies
from .gemini_service import GeminiService

logger = logging.getLogger("gemini_proxy.cookie_admin")


async def apply_cookie_payload(
    service: GeminiService,
    cookie_store: CookieStore,
    raw: str | bytes,
    *,
    reinit: bool = True,
) -> dict[str, Any]:
    """Parse, persist, and hot-swap. Raises ``ValueError`` if nothing
    cookie-shaped is found; the store/client are left untouched in that case."""
    parsed = parse_cookies(raw)  # ValueError on garbage
    if not cookie_store.path:
        raise ValueError("no cookie_file configured to write to")

    as_objects = [{"name": k, "value": v} for k, v in parsed.items()]
    cookie_store.path.parent.mkdir(parents=True, exist_ok=True)
    cookie_store.path.write_text(json.dumps(as_objects, indent=2), encoding="utf-8")
    logger.info("Applied %d cookies to %s", len(parsed), cookie_store.path)

    await service.reset()

    result: dict[str, Any] = {
        "applied": True,
        "cookie_count": len(parsed),
        "session_cookie_present": "__Secure-1PSID" in parsed,
        "path": str(cookie_store.path),
    }
    if reinit:
        try:
            await service.get_client()
            result["reinit_ok"] = True
            result["cookie_mode"] = service.cookie_mode
        except Exception as exc:  # noqa: BLE001
            result["reinit_ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result
