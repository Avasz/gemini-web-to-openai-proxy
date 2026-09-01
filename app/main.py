"""FastAPI application factory and the observability endpoints."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .activity_log import ActivityLog
from .admin import STATIC_DIR, router as admin_router
from .admin_auth import attach_admin_session, require_admin, resolve_admin_credential
from .concurrency import GenerationGate
from .config import Config, load_config
from .cookie_watcher import CookieWatcher
from .cookies import CookieStore
from .dotenv import load_dotenv
from .gemini_service import GeminiService
from .google_api import router as google_router
from .openai_api import router as openai_router
from .responses_api import router as responses_router
from .self_heal import SessionHealer
from .sessions_api import router as sessions_router
from .status_report import build_full_status
from .warm_sessions import WarmSessionManager

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

    data_dir = cfg.resolve_path(cfg.data_dir)
    activity = ActivityLog(
        data_dir / "activity.db",
        retention_days=float(getattr(cfg, "activity_log_retention_days", 7)),
    )
    gemini.activity = activity

    gemini.gate = GenerationGate(
        int(getattr(cfg, "max_concurrent_generations", 3)),
        float(getattr(cfg, "slot_wait_timeout", 60.0)),
    )

    warm_sessions = WarmSessionManager(
        gemini,
        idle_timeout=float(getattr(cfg, "warm_session_idle_timeout", 900.0)),
        max_sessions=int(getattr(cfg, "max_warm_sessions", 20)),
    )

    admin_credential = resolve_admin_credential(
        data_dir, getattr(cfg, "admin_username", "admin")
    )
    cookie_watcher = CookieWatcher(gemini, cookie_store, cfg)
    # no point re-initing a deliberately-anonymous session
    heal_interval = 0.0 if getattr(cfg, "force_anonymous", False) else float(
        getattr(cfg, "self_heal_interval", 600.0)
    )
    healer = SessionHealer(gemini, heal_interval)
    cookie_watcher.on_new_session = healer.nudge

    if not cookie_store.has_session_cookies():
        logger.warning(
            "No session cookie found at %s: service will run in anonymous/guest tier.",
            cookie_path,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await activity.start()
        await cookie_watcher.start()
        await healer.start()
        yield
        await healer.stop()
        await cookie_watcher.stop()
        await activity.stop()
        if gemini.is_ready():
            await gemini.reset()

    docs_access = str(getattr(cfg, "docs_access", "admin")).lower()
    if docs_access not in ("admin", "open", "disabled"):
        logger.warning("Unknown docs_access %r, falling back to 'admin'.", docs_access)
        docs_access = "admin"
    status_access = str(getattr(cfg, "status_access", "admin")).lower()
    if status_access not in ("admin", "open", "disabled"):
        logger.warning("Unknown status_access %r, falling back to 'admin'.", status_access)
        status_access = "admin"

    app = FastAPI(
        title="Gemini Web -> OpenAI-compatible API gateway",
        version=__version__,
        lifespan=lifespan,
        # Auto docs are re-added below (or not) based on docs_access, so they
        # can be gated behind the admin credential instead of always open.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.cookie_store = cookie_store
    app.state.gemini = gemini
    app.state.activity = activity
    app.state.admin_credential = admin_credential
    app.state.warm_sessions = warm_sessions
    app.state.cookie_watcher = cookie_watcher
    app.state.healer = healer
    app.state.started_at = time.time()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(openai_router)
    app.include_router(responses_router)
    app.include_router(google_router)
    app.include_router(sessions_router)
    app.include_router(admin_router)

    docs_deps = [Depends(require_admin)] if docs_access == "admin" else []
    if docs_access != "disabled":

        @app.get("/openapi.json", include_in_schema=False, dependencies=docs_deps)
        async def openapi_json() -> JSONResponse:
            return JSONResponse(app.openapi())

        @app.get("/docs", include_in_schema=False, dependencies=docs_deps)
        async def swagger_ui():
            return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")

        @app.get("/redoc", include_in_schema=False, dependencies=docs_deps)
        async def redoc_ui():
            return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        """Browsers get the admin dashboard (behind the admin credential);
        everything else gets an unauthenticated API index."""
        if "text/html" in request.headers.get("accept", ""):
            await require_admin(request)
            return attach_admin_session(
                FileResponse(STATIC_DIR / "index.html", media_type="text/html"), request
            )
        return {
            "name": "gemini-openai-proxy",
            "version": __version__,
            "links": {
                "openapi_docs": "/docs",
                "health": "/status",
                "liveness": "/healthz",
                "admin_dashboard": "/admin",
                "openai_base_url": "/v1",
                "google_base_url": "/v1beta",
            },
        }

    @app.get("/healthz")
    async def healthz() -> dict:
        """Liveness only: the process is up. Does not touch Gemini."""
        return {"status": "ok", "version": __version__}

    status_deps = [Depends(require_admin)] if status_access == "admin" else []
    if status_access != "disabled":

        @app.get("/status", dependencies=status_deps)
        async def status() -> dict:
            """Machine-readable health (SRS 2.7): the three independent
            signals plus the request-history summary and live model/quota
            info. Gated behind the admin credential by default; see
            status_access in the config."""
            return await build_full_status(app)

    return app


app = create_app()
