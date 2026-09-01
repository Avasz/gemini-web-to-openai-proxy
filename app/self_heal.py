"""Background self-healer for a degraded Gemini session.

``gemini_webapi``'s own ``auto_refresh`` loop bails out the moment
``account_status != AVAILABLE`` -- so a client that inits (or drifts) into the
degraded UNAUTHENTICATED state never recovers on its own. This task re-inits the
client on an interval until it comes back AVAILABLE.

Deliberately conservative: SRS 7 warns that rapid re-authentication pushes a real
account further into a degraded state, so re-init attempts are spaced minutes
apart and each consecutive failure doubles the wait (capped at 1h).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .gemini_service import GeminiService

logger = logging.getLogger("gemini_proxy.self_heal")

_MAX_BACKOFF = 3600.0


class SessionHealer:
    def __init__(self, service: GeminiService, interval: float):
        self._service = service
        self._base = float(interval)
        self._task: asyncio.Task | None = None
        self._backoff = self._base
        self._next_ok_at = 0.0
        self.attempts = 0
        self.recoveries = 0
        self.last_attempt_at: float | None = None
        self.last_result: str | None = None

    async def start(self) -> None:
        if self._base > 0 and self._task is None:
            self._task = asyncio.create_task(self._run(), name="session-healer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def nudge(self) -> None:
        """After a manual cookie import: drop the backoff so the next poll can
        attempt a heal immediately if the fresh cookies still landed degraded."""
        self._backoff = self._base
        self._next_ok_at = 0.0

    def stats(self) -> dict:
        return {
            "enabled": self._base > 0,
            "interval": self._base,
            "attempts": self.attempts,
            "recoveries": self.recoveries,
            "last_attempt_at": self.last_attempt_at,
            "last_result": self.last_result,
        }

    async def _run(self) -> None:
        poll = min(self._base, 120.0)
        while True:
            await asyncio.sleep(poll)
            if not self._service.is_ready() or self._service.client_authenticated is not False:
                self._backoff = self._base
                continue
            if time.time() < self._next_ok_at:
                continue

            self.attempts += 1
            self.last_attempt_at = time.time()
            logger.info(
                "session degraded (attempt %d) -- re-initialising the Gemini client",
                self.attempts,
            )
            try:
                await self._service.reset()
                await self._service.get_client()
            except Exception as exc:  # noqa: BLE001
                self.last_result = f"error: {exc}"
            else:
                self.last_result = (
                    "recovered" if self._service.client_authenticated else "still degraded"
                )

            if self._service.client_authenticated:
                logger.info("session recovered")
                self.recoveries += 1
                self._backoff = self._base
                self._next_ok_at = 0.0
            else:
                self._next_ok_at = time.time() + self._backoff
                logger.warning(
                    "session still degraded; next self-heal attempt in >= %.0fs",
                    self._backoff,
                )
                self._backoff = min(self._backoff * 2, _MAX_BACKOFF)
