"""Local request history (SRS 2.7).

Every generation attempt is recorded to a single-file SQLite database. Writes are
fire-and-forget on a background worker task -- they never block or slow the
request path, and a write failure never propagates to the caller.

``summary()`` rolls the trailing window up for the health endpoint and dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("gemini_proxy.activity")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    surface TEXT,
    model_requested TEXT,
    model_served TEXT,
    ok INTEGER NOT NULL,
    error_code TEXT,
    latency_ms REAL,
    prompt_chars INTEGER,
    reply_chars INTEGER,
    streamed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""


@dataclass
class RequestRecord:
    ts: float
    surface: str
    model_requested: str
    model_served: str | None
    ok: bool
    error_code: str | None
    latency_ms: float
    prompt_chars: int
    reply_chars: int
    streamed: bool


class ActivityLog:
    def __init__(self, db_path: str | Path, retention_days: float = 7.0):
        self._path = Path(db_path)
        self._retention_s = max(retention_days, 0.0) * 86400
        self._queue: asyncio.Queue[RequestRecord | None] = asyncio.Queue(maxsize=1000)
        self._worker: asyncio.Task | None = None
        self._writes_since_prune = 0
        self._enabled = True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._connect().close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("activity log disabled (cannot open %s): %s", self._path, exc)
            self._enabled = False

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.executescript(_SCHEMA)
        return conn

    async def start(self) -> None:
        if self._enabled and self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="activity-log-writer")

    async def stop(self) -> None:
        if self._worker is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._worker, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._worker.cancel()
        self._worker = None

    def record(self, rec: RequestRecord) -> None:
        """Non-blocking. Drops the record rather than ever blocking the caller."""
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            logger.debug("activity log queue full; dropping a record")

    async def _run(self) -> None:
        while True:
            rec = await self._queue.get()
            try:
                if rec is None:
                    return
                try:
                    await asyncio.to_thread(self._write, rec)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("activity log write failed: %s", exc)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait for all queued records to be written (used by tests)."""
        if self._enabled and self._worker is not None:
            await self._queue.join()

    def _write(self, rec: RequestRecord) -> None:
        conn = self._connect()
        try:
            d = asdict(rec)
            conn.execute(
                "INSERT INTO requests (ts,surface,model_requested,model_served,ok,"
                "error_code,latency_ms,prompt_chars,reply_chars,streamed) "
                "VALUES (:ts,:surface,:model_requested,:model_served,:ok,:error_code,"
                ":latency_ms,:prompt_chars,:reply_chars,:streamed)",
                {**d, "ok": int(d["ok"]), "streamed": int(d["streamed"])},
            )
            conn.commit()
            self._writes_since_prune += 1
            if self._retention_s and self._writes_since_prune >= 200:
                conn.execute(
                    "DELETE FROM requests WHERE ts < ?", (time.time() - self._retention_s,)
                )
                conn.commit()
                self._writes_since_prune = 0
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    async def summary(self, window_hours: float = 24.0) -> dict[str, Any]:
        if not self._enabled:
            return {"enabled": False}
        try:
            return await asyncio.to_thread(self._summary, window_hours)
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": str(exc)}

    def _summary(self, window_hours: float) -> dict[str, Any]:
        since = time.time() - window_hours * 3600
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*), SUM(ok), AVG(latency_ms), MAX(ts) "
                "FROM requests WHERE ts >= ?",
                (since,),
            ).fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            avg_latency = row[2]
            last_ts = row[3]
            per_model: dict[str, dict[str, int]] = {}
            for served, cnt, okc in conn.execute(
                "SELECT COALESCE(model_served, model_requested), COUNT(*), SUM(ok) "
                "FROM requests WHERE ts >= ? GROUP BY 1 ORDER BY 2 DESC",
                (since,),
            ):
                per_model[served or "?"] = {"count": cnt, "ok": okc or 0}
            errors_by_code: dict[str, int] = {}
            for code, cnt in conn.execute(
                "SELECT error_code, COUNT(*) FROM requests "
                "WHERE ts >= ? AND ok = 0 GROUP BY error_code",
                (since,),
            ):
                errors_by_code[code or "unknown"] = cnt
        finally:
            conn.close()

        errors = total - ok
        return {
            "enabled": True,
            "window_hours": window_hours,
            "total": total,
            "ok": ok,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "avg_latency_ms": round(avg_latency, 1) if avg_latency is not None else None,
            "last_request_at": last_ts,
            "seconds_since_last": round(time.time() - last_ts, 1) if last_ts else None,
            "per_model": per_model,
            "errors_by_code": errors_by_code,
        }
