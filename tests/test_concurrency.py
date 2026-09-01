import asyncio

import httpx
import pytest

from app.concurrency import GenerationGate
from app.config import Config
from app.errors import UpstreamError
from app.main import create_app


async def test_gate_limits_and_tracks():
    gate = GenerationGate(limit=2, slot_wait_timeout=5.0)
    peak = 0
    now = 0

    async def worker():
        nonlocal peak, now
        async with gate.slot():
            now += 1
            peak = max(peak, now)
            await asyncio.sleep(0.05)
            now -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak == 2
    assert gate.in_flight == 0 and gate.waiting == 0


async def test_gate_rejects_when_no_slot_frees_in_time():
    gate = GenerationGate(limit=1, slot_wait_timeout=0.1)

    async def hold():
        async with gate.slot():
            await asyncio.sleep(0.5)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.02)
    with pytest.raises(UpstreamError) as ei:
        async with gate.slot():
            pass
    assert ei.value.status_code == 503
    assert ei.value.code == "capacity"
    await holder
    assert gate.rejected == 1


async def _post(client, i):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "gemini-flash", "messages": [{"role": "user", "content": f"q{i}"}]},
    )


async def test_endpoint_caps_concurrent_generations(tmp_path, monkeypatch, fake_client):
    from app import gemini_service

    async def fake_get_client(self):
        self._client = fake_client
        self._cookie_mode = "anonymous"
        return fake_client

    monkeypatch.setattr(gemini_service.GeminiService, "get_client", fake_get_client)
    fake_client.generate_delay = 0.2

    app = create_app(Config(
        {"cookie_file": str(tmp_path / "c.json"), "data_dir": str(tmp_path / "d"),
         "max_concurrent_generations": 2, "slot_wait_timeout": 10},
        None,
    ))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            results = await asyncio.gather(*(_post(c, i) for i in range(6)))

    assert all(r.status_code == 200 for r in results)
    assert fake_client.concurrent_peak == 2  # never more than the cap upstream


async def test_status_reports_capacity(tmp_path, monkeypatch, fake_client):
    from app import gemini_service

    async def fake_get_client(self):
        self._client = fake_client
        return fake_client

    monkeypatch.setattr(gemini_service.GeminiService, "get_client", fake_get_client)
    app = create_app(Config(
        {"cookie_file": str(tmp_path / "c.json"), "data_dir": str(tmp_path / "d"),
         "max_concurrent_generations": 4},
        None,
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/status")
    assert r.json()["capacity"]["limit"] == 4
    assert r.json()["capacity"]["in_flight"] == 0
