"""Bound concurrent generations against the single shared upstream connection
(SRS 2.8).

The one shared ``gemini_webapi`` connection does not survive many long-running
generations piling onto it at once -- under load it stops receiving data for
minutes and then breaks, taking every piled-on request with it. So only a small
number run at a time; the rest wait for a slot (not serialised to 1 -- this
service backs several independent callers).

A request that can't get a slot within ``slot_wait_timeout`` is rejected with a
clean 503 rather than hanging.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from .errors import UpstreamError

logger = logging.getLogger("gemini_proxy.concurrency")


class GenerationGate:
    def __init__(self, limit: int, slot_wait_timeout: float):
        self.limit = max(1, int(limit))
        if int(limit) < 2:
            logger.warning(
                "max_concurrent_generations=%s serialises all callers behind the "
                "slowest request; 2-4 is recommended (SRS 2.8).",
                limit,
            )
        self._sem = asyncio.Semaphore(self.limit)
        self._wait_timeout = float(slot_wait_timeout)
        self.in_flight = 0
        self.waiting = 0
        self.rejected = 0

    @asynccontextmanager
    async def slot(self):
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), self._wait_timeout)
        except asyncio.TimeoutError:
            self.rejected += 1
            raise UpstreamError(
                f"Server at capacity: {self.limit} generations already in flight and "
                f"no slot freed within {self._wait_timeout:.0f}s. Retry shortly.",
                503,
                "capacity",
            )
        finally:
            self.waiting -= 1

        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self._sem.release()

    def stats(self) -> dict[str, int]:
        return {
            "limit": self.limit,
            "in_flight": self.in_flight,
            "waiting": self.waiting,
            "rejected_total": self.rejected,
        }
