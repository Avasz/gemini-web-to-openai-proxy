"""FastAPI application factory and the observability endpoints."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .activity_log import ActivityLog
from .config import Config, load_config
from .cookies import CookieStore
from .dotenv import load_dotenv
from .gemini_service import GeminiService
from .google_api import router as google_router
from .health import build_health
from .openai_api import router as openai_router
from .responses_api import router as responses_router

logging.basicConfig(
    level=os.environ.get("GEMINI_PROXY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("gemini_proxy")


def create_app(config: Config | None = None) -> FastAPI:
    load_dotenv()
    cfg = config or load_config()

    if not cfg.api_keys:
        logger.warning(
            "No api_keys configured: generation endpoints are OPEN."
        )

    cookie_path = cfg.resolve_path(cfg.cookie_file) if cfg.cookie_file else None
    cookie_store = CookieStore(cookie_path)
    gemini = GeminiService(cfg, cookie_store)

    activity = ActivityLog(
        cfg.resolve_path(cfg.data_dir) / "activity.db",
        retention_days=float(getattr(cfg, "activity_log_retention_days", 7)),
    )
    gemini.activity = activity

    if not cookie_store.has_session_cookies():
        logger.warning(
            "No session cookie found at %s: service will run in anonymous/guest tier.",
            cookie_path,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await activity.start()
        yield
        await activity.stop()
        if gemini.is_ready():
            await gemini.reset()

    app = FastAPI(
        title="Gemini Web -> OpenAI-compatible API gateway",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.cookie_store = cookie_store
    app.state.gemini = gemini
    app.state.activity = activity

    app.include_router(openai_router)
    app.include_router(responses_router)
    app.include_router(google_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Liveness only: the process is up. Does not touch Gemini."""
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    async def status() -> dict:
        """Machine-readable health (SRS 2.7): three independent signals plus the
        local request-history summary. Attempts a lazy client init so the report
        reflects whether the configured credentials actually work."""
        try:
            await gemini.get_client()
        except Exception:  # noqa: BLE001 - detail is in the snapshot / health
            pass
        return {
            "version": __version__,
            "config_source": str(cfg.source_path) if cfg.source_path else "defaults",
            "health": await build_health(gemini, activity),
            "gemini": await gemini.status_snapshot(),
            "activity": await activity.summary(24.0),
        }

    return app


app = create_app()
