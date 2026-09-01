"""The full status document served to the dashboard and to ``/admin/status.json``.

Bundles the three health signals (SRS 2.7), the client/cookie snapshot, the
concurrency gate, the 24h request-history summary, and -- for the dashboard --
the live model list, account quota/usage, and warm-session counts.
"""

from __future__ import annotations

import time
from typing import Any

from . import __version__
from .health import build_health


async def build_full_status(app) -> dict[str, Any]:
    state = app.state
    gemini = state.gemini
    activity = state.activity

    try:
        await gemini.get_client()
    except Exception:  # noqa: BLE001 - the detail is in health / snapshot
        pass

    cfg = state.config
    client = gemini._client  # noqa: SLF001
    models: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    default_model = getattr(cfg, "default_model", None)
    if client is not None:
        try:
            models = [
                {
                    "name": m.model_name,
                    "display_name": m.display_name,
                    "is_available": m.is_available,
                }
                for m in (client.list_models() or [])
            ]
        except Exception:  # noqa: BLE001
            pass
        try:
            if default_model:
                default_model = client.resolve_model(default_model).display_name
        except Exception:  # noqa: BLE001
            pass
        try:
            usage = client.usage_info or None
            if usage is None and client.quotas:
                usage = {"quotas": client.quotas}
        except Exception:  # noqa: BLE001
            pass

    warm = getattr(state, "warm_sessions", None)
    warm_block = None
    if warm is not None:
        warm_block = {
            "active": len(warm.list()),
            "idle_timeout": float(getattr(cfg, "warm_session_idle_timeout", 900.0)),
            "max": int(getattr(cfg, "max_warm_sessions", 20)),
        }

    watcher = getattr(state, "cookie_watcher", None)
    inbox = {
        "path": (
            getattr(watcher, "watch_file_path", None)
            or (str(state.cookie_store.path) if state.cookie_store.path else None)
        ),
        "last_import_at": getattr(watcher, "last_mirror_at", None) if watcher else None,
        "last_import_count": getattr(watcher, "last_mirror_count", None) if watcher else None,
    }

    started = getattr(state, "started_at", None)
    return {
        "version": __version__,
        "config_source": (
            str(state.config.source_path) if state.config.source_path else "defaults"
        ),
        "uptime_seconds": round(time.time() - started) if started else None,
        "default_model": default_model,
        "api_keys_required": bool(getattr(cfg, "api_keys", []) or []),
        "temporary_chat_default": bool(getattr(cfg, "temporary_chat_default", False)),
        "health": await build_health(gemini, activity),
        "gemini": await gemini.status_snapshot(),
        "capacity": gemini.gate.stats() if gemini.gate else None,
        "self_heal": state.healer.stats() if getattr(state, "healer", None) else None,
        "activity": await activity.summary(24.0),
        "models": models,
        "usage": usage,
        "warm_sessions": warm_block,
        "cookie_inbox": inbox,
    }
