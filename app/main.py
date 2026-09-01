"""FastAPI application factory and the Phase 1 health endpoints."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .config import Config, load_config
from .cookies import CookieStore
from .dotenv import load_dotenv
from .gemini_service import GeminiService
from .google_api import router as google_router
from .openai_api import router as openai_router

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
            "No api_keys configured: generation endpoints will be OPEN once implemented."
        )

    cookie_path = cfg.resolve_path(cfg.cookie_file) if cfg.cookie_file else None
    cookie_store = CookieStore(cookie_path)
    gemini = GeminiService(cfg, cookie_store)

    if not cookie_store.has_session_cookies():
        logger.warning(
            "No session cookie found at %s: service will run in anonymous/guest tier.",
            cookie_path,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
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

    app.include_router(openai_router)
    app.include_router(google_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Liveness only: the process is up. Does not touch Gemini."""
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    async def status() -> dict:
        """Phase 1 health view (SRS 2.7). Attempts a lazy client init so the
        report reflects whether the configured credentials actually work.
        """
        gemini_up = False
        try:
            await gemini.get_client()
            gemini_up = True
        except Exception:  # noqa: BLE001 - detail is in the snapshot
            gemini_up = False
        snapshot = await gemini.status_snapshot()
        return {
            "version": __version__,
            "config_source": str(cfg.source_path) if cfg.source_path else "defaults",
            "gemini_client_initialized": gemini_up,
            "gemini": snapshot,
        }

    return app


app = create_app()
