import pytest

from app.model_selection import resolve
from app.warm_sessions import SessionError, WarmSessionManager
from tests.conftest import FakeClient


class _Svc:
    def __init__(self, client):
        self._c = client
        self.generation = 0
        self._cbs = []

    def on_reset(self, cb):
        self._cbs.append(cb)

    async def get_client(self):
        return self._c

    def bump(self):
        self.generation += 1
        for cb in self._cbs:
            cb()


async def test_create_sends_one_priming_message():
    client = FakeClient()
    svc = _Svc(client)
    mgr = WarmSessionManager(svc, idle_timeout=100)
    resolved = resolve(client, "gemini-flash")
    sess = await mgr.create(resolved, "prime me")
    assert sess.model_name == "gemini-flash"
    assert sess.turns == 1
    assert sess.chat.messages == ["prime me"]          # a real message was sent
    assert mgr.get(sess.id) is sess


async def test_unknown_session_is_clean_error_not_fallback():
    mgr = WarmSessionManager(_Svc(FakeClient()), idle_timeout=100)
    with pytest.raises(SessionError):
        mgr.get("sess_nope")


async def test_session_invalidated_on_client_reset():
    client = FakeClient()
    svc = _Svc(client)
    mgr = WarmSessionManager(svc, idle_timeout=100)
    sess = await mgr.create(resolve(client, "gemini-flash"), None)
    svc.bump()  # simulate GeminiService.reset()
    with pytest.raises(SessionError):
        mgr.get(sess.id)


async def test_idle_pruning():
    import time as _t

    client = FakeClient()
    mgr = WarmSessionManager(_Svc(client), idle_timeout=0.01)
    sess = await mgr.create(resolve(client, "gemini-flash"), None)
    _t.sleep(0.05)
    with pytest.raises(SessionError):
        mgr.get(sess.id)


async def test_max_sessions_evicts_oldest():
    client = FakeClient()
    mgr = WarmSessionManager(_Svc(client), idle_timeout=100, max_sessions=2)
    a = await mgr.create(resolve(client, "gemini-flash"), None)
    b = await mgr.create(resolve(client, "gemini-flash"), None)
    c = await mgr.create(resolve(client, "gemini-flash"), None)
    assert mgr.get(b.id) and mgr.get(c.id)
    with pytest.raises(SessionError):
        mgr.get(a.id)


# ---- endpoint integration -------------------------------------------------- #

def test_sessions_endpoints_and_routing(client_factory, fake_client):
    with client_factory() as c:
        r = c.post("/v1/sessions", json={"model": "gemini-pro", "priming_message": "hi"})
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert r.json()["model"] == "gemini-pro"

        assert any(s["session_id"] == sid for s in c.get("/v1/sessions").json()["data"])

        # a generation routed through the session uses chat= and the fixed model
        g = c.post("/v1/chat/completions",
                   json={"session_id": sid, "messages": [{"role": "user", "content": "next"}]})
        assert g.status_code == 200
        assert g.json()["model"] == "gemini-pro"
        assert fake_client.calls[-1]["chat"] is not None
        assert c.get(f"/v1/sessions/{sid}").json()["turns"] == 2

        d = c.delete(f"/v1/sessions/{sid}")
        assert d.status_code == 200
        assert c.get(f"/v1/sessions/{sid}").status_code == 404


def test_unknown_session_id_in_generation_is_409(client_factory):
    with client_factory() as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sess_bogus",
                         "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 409


def test_request_without_session_is_unaffected(client_factory, fake_client):
    with client_factory() as c:
        r = c.post("/v1/chat/completions",
                   json={"model": "gemini-flash", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
        assert fake_client.calls[-1]["chat"] is None
        assert fake_client.calls[-1]["prompt"] == "User:\nx"  # role-flattened (stateless)


def test_session_sends_bare_trailing_turn_not_role_flattened(client_factory, fake_client):
    with client_factory() as c:
        sid = c.post("/v1/sessions", json={"model": "gemini-flash"}).json()["session_id"]
        c.post("/v1/chat/completions", json={
            "session_id": sid,
            "messages": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "the new question"},
            ],
        })
    sent = fake_client.calls[-1]["prompt"]
    assert sent == "the new question"          # no "User:\n", only the trailing turn
    assert "earlier" not in sent
