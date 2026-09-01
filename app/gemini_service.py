"""The one shared, lazily-initialized Gemini client per process (SRS 1.3).

Wraps ``gemini_webapi.GeminiClient``:
  * builds the client from the full cookie set when present, or with no cookies at
    all (the library has a genuine guest/anonymous fallback -- SRS 2.2);
  * initializes it once, on first use, guarded by a lock;
  * can be torn down and rebuilt so a fresh cookie import takes effect without a
    process restart (SRS 2.9).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from curl_cffi.requests import Cookies
from gemini_webapi import GeminiClient

from .config import Config
from .cookies import CookieStore

logger = logging.getLogger("gemini_proxy.gemini")


class GeminiService:
    def __init__(self, config: Config, cookie_store: CookieStore):
        self._config = config
        self._cookies = cookie_store
        self._client: GeminiClient | None = None
        self._lock = asyncio.Lock()
        self._init_error: str | None = None
        self._cookie_mode: str = "uninitialized"
        # request-history sink + concurrency gate, set by the app factory; None in unit tests
        self.activity: Any = None
        self.gate: Any = None
        self._force_anonymous = bool(getattr(config, "force_anonymous", False))
        self._auto_refresh = not self._force_anonymous and bool(
            getattr(config, "auto_refresh", True)
        )
        # The library reads its rotated-cookie cache dir from $GEMINI_COOKIE_PATH
        # lazily on each call, so setting it here (before the first init) is enough.
        # Precedence: explicit `cookie_cache_dir` config > pre-set $GEMINI_COOKIE_PATH
        # env var > {data_dir}/gemini_webapi (a single mountable volume, SRS 3).
        # Forced-anonymous always uses a dedicated sub-dir that never saw an
        # authenticated session (SRS 7 -- avoid a stale-cache false positive).
        explicit = getattr(config, "cookie_cache_dir", None)
        if self._force_anonymous:
            self._cookie_cache_dir = config.resolve_path(
                getattr(config, "data_dir", "data")
            ) / "gemini_webapi_anon"
        elif explicit:
            self._cookie_cache_dir = config.resolve_path(explicit)
        elif os.environ.get("GEMINI_COOKIE_PATH"):
            self._cookie_cache_dir = Path(os.environ["GEMINI_COOKIE_PATH"]).expanduser()
        else:
            self._cookie_cache_dir = config.resolve_path(
                getattr(config, "data_dir", "data")
            ) / "gemini_webapi"
        self._cookie_cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["GEMINI_COOKIE_PATH"] = str(self._cookie_cache_dir)

    @property
    def cookie_mode(self) -> str:
        """``authenticated`` (cookies were supplied) or ``anonymous`` (guest tier),
        or ``uninitialized`` / ``error``."""
        return self._cookie_mode

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def is_ready(self) -> bool:
        return self._client is not None

    def _build_client(self) -> tuple[GeminiClient, str]:
        cookies = {} if self._force_anonymous else self._cookies.load()
        psid = cookies.get("__Secure-1PSID")
        psidts = cookies.get("__Secure-1PSIDTS")
        client = GeminiClient(psid, psidts)
        mode = "anonymous(forced)" if self._force_anonymous else "anonymous"
        if psid:
            mode = "authenticated"
            # Pass the whole google.com cookie set through, not just the two the
            # constructor wires up (SRS 2.2). The refresh path needs the rest.
            jar = Cookies()
            for name, value in cookies.items():
                jar.set(name, value, domain=".google.com", secure=True)
            client._cookies = jar  # noqa: SLF001 - library exposes no public setter
        return client, mode

    async def get_client(self) -> GeminiClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            client, mode = self._build_client()
            try:
                await client.init(
                    timeout=float(self._config.connection_timeout),
                    auto_close=False,
                    auto_refresh=self._auto_refresh,
                    refresh_interval=float(self._config.cookie_refresh_interval),
                    watchdog_timeout=float(self._config.zombie_stream_timeout),
                )
            except Exception as exc:  # noqa: BLE001 - surfaced via health endpoint
                self._init_error = f"{type(exc).__name__}: {exc}"
                self._cookie_mode = "error"
                logger.error("Gemini client init failed: %s", self._init_error)
                raise
            self._client = client
            self._cookie_mode = mode
            self._init_error = None
            logger.info("Gemini client initialized (%s mode)", mode)
            return client

    async def reset(self) -> None:
        """Tear down the current client so the next request rebuilds it from the
        current cookie file (SRS 2.9)."""
        async with self._lock:
            old = self._client
            self._client = None
            self._cookie_mode = "uninitialized"
            self._init_error = None
        if old is not None:
            try:
                await old.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing old Gemini client: %s", exc)

    async def status_snapshot(self) -> dict[str, Any]:
        """Best-effort view of the client for the health endpoint (SRS 2.7).

        Phase 1 keeps this lightweight; the three-way split lands in the
        observability phase.
        """
        snap: dict[str, Any] = {
            "ready": self.is_ready(),
            "cookie_mode": self._cookie_mode,
            "force_anonymous": self._force_anonymous,
            "init_error": self._init_error,
            "cookie_file_present": bool(
                self._cookies.path and self._cookies.path.is_file()
            ),
            "session_cookie_present": self._cookies.has_session_cookies(),
            "cookie_cache_dir": str(self._cookie_cache_dir),
        }
        if self._client is not None:
            snap["access_token_present"] = bool(getattr(self._client, "access_token", None))
            snap["running"] = bool(getattr(self._client, "_running", False))
            # Which candidate cookie group actually authenticated: "Cache",
            # "Base Cookies", "Browser (...)", or a guest group (SRS 2.7 / 7).
            snap["cookie_source"] = getattr(self._client, "_cookie_source", "") or None
        return snap
