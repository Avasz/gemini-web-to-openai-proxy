"""Background cookie-file watcher (SRS 2.9).

Two jobs, on a poll interval:
  * if ``cookie_watch_file`` is configured and its contents change, mirror them
    into ``cookie_file`` (a drop-a-file-to-recover path, separate from the
    dashboard paste form);
  * whenever ``cookie_file``'s ``__Secure-1PSID`` changes (a genuinely new
    session was pasted/dropped in), tear down and rebuild the client.

It deliberately reacts only to a *session* change, not to every file touch or
``__Secure-1PSIDTS`` rotation -- needless cold re-inits degrade the account.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Config
from .cookies import CookieStore
from .gemini_service import GeminiService

logger = logging.getLogger("gemini_proxy.cookie_watcher")


class CookieWatcher:
    def __init__(self, service: GeminiService, cookie_store: CookieStore, cfg: Config):
        self._service = service
        self._store = cookie_store
        self._interval = float(getattr(cfg, "cookie_watch_interval", 15.0) or 0)
        wf = getattr(cfg, "cookie_watch_file", None)
        self._watch_file = cfg.resolve_path(wf) if wf else None
        self._task: asyncio.Task | None = None
        self._last_psid = self._store.load().get("__Secure-1PSID", "")
        self._last_watch_sig: tuple[float, int] | None = None
        self.watch_file_path = str(self._watch_file) if self._watch_file else None
        self.last_mirror_at: float | None = None
        self.last_mirror_count: int | None = None
        self.on_new_session = None  # optional callback, set by the app factory

    async def start(self) -> None:
        if self._interval <= 0 or self._store.path is None:
            return
        self._task = asyncio.create_task(self._run(), name="cookie-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("cookie watcher tick failed: %s", exc)

    async def _tick(self) -> None:
        if self._watch_file and self._watch_file.is_file():
            st = self._watch_file.stat()
            sig = (st.st_mtime, st.st_size)
            if sig != self._last_watch_sig:
                self._last_watch_sig = sig
                self._mirror_watch_file()

        psid = self._store.load().get("__Secure-1PSID", "")
        if psid != self._last_psid:
            self._last_psid = psid
            logger.info(
                "cookie file session changed (new __Secure-1PSID); rebuilding client"
            )
            await self._service.reset()
            if callable(self.on_new_session):
                try:
                    self.on_new_session()
                except Exception:  # noqa: BLE001
                    pass

    def _mirror_watch_file(self) -> None:
        from .cookies import parse_cookies
        import json

        try:
            parsed = parse_cookies(self._watch_file.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError) as exc:
            logger.warning("watch file %s not usable: %s", self._watch_file, exc)
            return
        target = self._store.path
        assert target is not None
        target.write_text(
            json.dumps([{"name": k, "value": v} for k, v in parsed.items()], indent=2),
            encoding="utf-8",
        )
        import time as _t

        self.last_mirror_at = _t.time()
        self.last_mirror_count = len(parsed)
        logger.info("Mirrored %d cookies from %s into %s",
                    len(parsed), self._watch_file, target)
