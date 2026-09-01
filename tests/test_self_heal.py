import asyncio

import pytest

from app.self_heal import SessionHealer


class _FakeService:
    def __init__(self):
        self._ready = True
        self.auth = False          # client_authenticated
        self.reset_calls = 0
        self.recover_after = None  # become authed after N reset+get cycles

    def is_ready(self):
        return self._ready

    @property
    def client_authenticated(self):
        return self.auth

    async def reset(self):
        self.reset_calls += 1

    async def get_client(self):
        if self.recover_after is not None and self.reset_calls >= self.recover_after:
            self.auth = True


async def _run_briefly(healer, seconds=0.6):
    await healer.start()
    await asyncio.sleep(seconds)
    await healer.stop()


async def test_heals_a_degraded_session(monkeypatch):
    svc = _FakeService()
    svc.recover_after = 1
    healer = SessionHealer(svc, interval=0.05)  # poll ~0.05s
    await _run_briefly(healer, 0.4)
    assert svc.auth is True
    assert svc.reset_calls >= 1
    assert healer.recoveries == 1
    assert healer.last_result == "recovered"


async def test_backs_off_while_still_degraded():
    svc = _FakeService()  # never recovers
    healer = SessionHealer(svc, interval=0.05)
    await _run_briefly(healer, 0.5)
    # attempts happen, but spaced by a growing backoff — not once per poll
    assert 1 <= healer.attempts <= 4
    assert healer.last_result == "still degraded"


async def test_does_nothing_when_authenticated():
    svc = _FakeService()
    svc.auth = True
    healer = SessionHealer(svc, interval=0.05)
    await _run_briefly(healer, 0.3)
    assert svc.reset_calls == 0
    assert healer.attempts == 0


async def test_disabled_when_interval_zero():
    svc = _FakeService()
    healer = SessionHealer(svc, interval=0.0)
    await healer.start()
    await asyncio.sleep(0.2)
    await healer.stop()
    assert svc.reset_calls == 0
    assert healer.stats()["enabled"] is False


async def test_nudge_clears_backoff():
    svc = _FakeService()
    healer = SessionHealer(svc, interval=0.05)
    await healer.start()
    await asyncio.sleep(0.3)
    a1 = healer.attempts
    healer.nudge()
    svc.recover_after = svc.reset_calls + 1
    await asyncio.sleep(0.3)
    await healer.stop()
    assert healer.attempts > a1
    assert svc.auth is True
