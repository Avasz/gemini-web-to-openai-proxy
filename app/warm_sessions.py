"""Optional reusable warm chat sessions (SRS 2.11).

Motivation: a cold single-shot request pays provider-side per-conversation setup
that a follow-up turn in an established conversation does not. A caller can start
a session once (which sends one real priming message -- constructing a handle
alone allocates nothing upstream) and then route later requests through it.

**Strictly opt-in.** A request that references no session behaves exactly as if
this module did not exist.

State is in memory only, pruned after an idle period, and every session is
invalidated when the shared client is torn down/rebuilt (a session tied to a dead
client must never be silently reused).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gemini_proxy.warm_sessions")


class SessionError(ValueError):
    """Unknown / expired / invalidated session -- surfaced as a clean 4xx,
    never a silent fallback to a fresh conversation (SRS 2.11)."""


@dataclass
class WarmSession:
    id: str
    chat: Any                      # gemini_webapi ChatSession
    model_name: str                # fixed for the session's lifetime
    generation: int                # GeminiService.generation it was created under
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    turns: int = 0

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "model": self.model_name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "turns": self.turns,
            "idle_seconds": round(time.time() - self.last_used_at, 1),
        }


class WarmSessionManager:
    def __init__(self, service, idle_timeout: float = 900.0, max_sessions: int = 20):
        self._service = service
        self._idle_timeout = float(idle_timeout)
        self._max = max(1, int(max_sessions))
        self._sessions: dict[str, WarmSession] = {}
        self._lock = asyncio.Lock()
        service.on_reset(self.invalidate_all)

    def invalidate_all(self) -> None:
        if self._sessions:
            logger.info("invalidating %d warm session(s) (client reset)", len(self._sessions))
        self._sessions.clear()

    def _prune(self) -> None:
        now = time.time()
        dead = [
            sid for sid, s in self._sessions.items()
            if now - s.last_used_at > self._idle_timeout
            or s.generation != self._service.generation
        ]
        for sid in dead:
            self._sessions.pop(sid, None)

    async def create(self, resolved, priming_message: str | None) -> WarmSession:
        client = await self._service.get_client()
        chat = client.start_chat(model=resolved.model)
        # a real sent-and-answered message is what actually allocates the
        # conversation upstream (SRS 2.11)
        await chat.send_message(priming_message or "Hello.")
        async with self._lock:
            self._prune()
            if len(self._sessions) >= self._max:
                oldest = min(self._sessions.values(), key=lambda s: s.last_used_at)
                self._sessions.pop(oldest.id, None)
            sid = f"sess_{uuid.uuid4().hex}"
            sess = WarmSession(
                id=sid, chat=chat, model_name=resolved.served_name,
                generation=self._service.generation, turns=1,
            )
            self._sessions[sid] = sess
        logger.info("warm session %s started (model=%s)", sid, resolved.served_name)
        return sess

    def get(self, session_id: str) -> WarmSession:
        self._prune()
        sess = self._sessions.get(session_id)
        if sess is None:
            raise SessionError(
                f"Unknown or expired session '{session_id}'. Start a new one via "
                f"POST /v1/sessions; do not expect a fresh conversation to be "
                f"substituted."
            )
        if sess.generation != self._service.generation:
            self._sessions.pop(session_id, None)
            raise SessionError(
                f"Session '{session_id}' was invalidated (the Gemini client was "
                f"rebuilt, e.g. after a cookie import). Start a new session."
            )
        return sess

    def touch(self, sess: WarmSession) -> None:
        sess.last_used_at = time.time()
        sess.turns += 1

    def close(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def list(self) -> list[dict[str, Any]]:
        self._prune()
        return [s.info() for s in self._sessions.values()]
