import time

import pytest

from app.activity_log import ActivityLog, RequestRecord


def _rec(**kw):
    base = dict(
        ts=time.time(), surface="chat.completions", model_requested="gemini-flash",
        model_served="gemini-flash", ok=True, error_code=None, latency_ms=12.0,
        prompt_chars=10, reply_chars=20, streamed=False,
    )
    base.update(kw)
    return RequestRecord(**base)


async def test_records_and_summarises(tmp_path):
    log = ActivityLog(tmp_path / "a.db", retention_days=7)
    await log.start()
    log.record(_rec())
    log.record(_rec(ok=False, error_code="session_unauthenticated", model_served=None))
    log.record(_rec(model_requested="gemini-pro-high", model_served="gemini-pro", latency_ms=50))
    await log.drain()

    s = await log.summary(24)
    assert s["total"] == 3
    assert s["ok"] == 2
    assert s["errors"] == 1
    assert s["error_rate"] == pytest.approx(0.3333, abs=1e-3)
    assert s["per_model"]["gemini-flash"]["count"] == 2
    assert s["per_model"]["gemini-pro"]["count"] == 1
    assert s["errors_by_code"]["session_unauthenticated"] == 1
    assert s["seconds_since_last"] is not None
    await log.stop()


async def test_window_excludes_old_rows(tmp_path):
    log = ActivityLog(tmp_path / "a.db")
    await log.start()
    log.record(_rec(ts=time.time() - 3 * 3600))  # 3h ago
    log.record(_rec())
    await log.drain()
    assert (await log.summary(1))["total"] == 1     # 1h window
    assert (await log.summary(24))["total"] == 2
    await log.stop()


async def test_record_never_raises_when_disabled(tmp_path):
    log = ActivityLog(tmp_path / "nested" / "deep" / "a.db")
    log._enabled = False
    log.record(_rec())  # must not raise
    assert (await log.summary())["enabled"] is False


async def test_generation_records_through_service(tmp_path, fake_client):
    """run_generation writes a history row (success and failure)."""
    from app.config import Config
    from app.cookies import CookieStore
    from app.gemini_service import GeminiService
    from app.generation import run_generation
    from app.translation import messages_to_prompt
    from gemini_webapi.exceptions import GeminiError

    cfg = Config({"data_dir": str(tmp_path)}, None)
    svc = GeminiService(cfg, CookieStore(None))
    svc.activity = ActivityLog(tmp_path / "activity.db")
    await svc.activity.start()

    async def fake_get_client(self=None):
        return fake_client

    svc.get_client = fake_get_client  # type: ignore[assignment]

    bundle = messages_to_prompt([{"role": "user", "content": "hi there"}])
    await run_generation(svc, "gemini-flash", bundle, temporary=False, surface="chat.completions")

    fake_client.raise_on_generate = GeminiError("usage limit exceeded")
    with pytest.raises(Exception):
        await run_generation(svc, "gemini-flash", bundle, temporary=False, surface="chat.completions")

    await svc.activity.drain()
    s = await svc.activity.summary(24)
    assert s["total"] == 2
    assert s["ok"] == 1
    assert s["errors"] == 1
    assert s["per_model"]["gemini-flash"]["count"] >= 1
    assert "usage_limit_exceeded" in s["errors_by_code"]
    await svc.activity.stop()
