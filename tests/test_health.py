from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app


def _client(tmp_path, **overrides):
    values = {"cookie_file": str(tmp_path / "cookies.json"), "data_dir": str(tmp_path / "data")}
    values.update(overrides)
    return TestClient(create_app(Config(values, None)))


def test_healthz(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_status_shape_without_network(tmp_path, monkeypatch):
    # Force client init to fail fast so the test never hits the network.
    from app import gemini_service

    async def boom(self):
        raise RuntimeError("offline test")

    monkeypatch.setattr(gemini_service.GeminiService, "get_client", boom)

    with _client(tmp_path) as c:
        r = c.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert body["health"]["overall"] == "down"
        assert body["health"]["client_authenticated"] is False
        assert body["gemini"]["session_cookie_present"] is False
        assert body["config_source"] == "defaults"
        assert body["activity"]["enabled"] is True
