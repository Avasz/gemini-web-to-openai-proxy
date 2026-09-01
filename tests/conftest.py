from __future__ import annotations

import pytest

from app.config import Config
from app.main import create_app


class FakeModel:
    def __init__(self, name, model_id, aliases=None, is_available=True):
        self.model_name = name
        self.model_id = model_id
        self.display_name = name.title()
        self.description = f"fake {name}"
        self.aliases = aliases or []
        self.is_available = is_available


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)


class FakeImage:
    def __init__(self, data=PNG_1PX, url="https://gemini/img/1"):
        self._data = data
        self.url = url

    async def save(self, path="temp", filename=None, verbose=False, **kw):
        import os
        import uuid as _u

        os.makedirs(path, exist_ok=True)
        p = os.path.join(path, f"{_u.uuid4().hex}.png")
        with open(p, "wb") as fh:
            fh.write(self._data)
        return p


class FakeOutput:
    def __init__(self, text, metadata=None, delta=None, images=None):
        self.text = text
        self.text_delta = delta or ""
        self.metadata = metadata or ["c_fake", "r_fake"]
        self.images = images or []


class FakeClient:
    def __init__(self):
        self._models = [
            FakeModel("gemini-flash", "aaa", ["flash"]),
            FakeModel("gemini-pro", "bbb", ["pro"]),
        ]
        self.usage_info = {"compute": "ok"}
        self.quotas = {}
        self.access_token = "tok"
        self._running = True
        self.calls = []
        self.raise_on_generate = None
        self.next_images = []  # FakeImage list returned by the next generation

    def list_models(self):
        return list(self._models)

    def resolve_model(self, name):
        for m in self._models:
            if name.lower() == m.model_name or name.lower() in m.aliases:
                return m
        raise ValueError(f"Unknown model name: '{name}'")

    async def generate_content(self, prompt, model=None, temporary=False,
                               extended_thinking=False, files=None, **kw):
        self.calls.append(
            {"prompt": prompt, "model": getattr(model, "model_name", model),
             "temporary": temporary, "extended_thinking": extended_thinking,
             "files": list(files) if files else None}
        )
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        return FakeOutput(f"echo: {prompt[:40]}", images=list(self.next_images))

    async def generate_content_stream(self, prompt, model=None, temporary=False,
                                      extended_thinking=False, files=None, **kw):
        self.calls.append({"stream": True, "extended_thinking": extended_thinking,
                           "files": list(files) if files else None})
        acc = ""
        for piece in ["Hello", " ", "world"]:
            acc += piece
            yield FakeOutput(acc, delta=piece)
        yield FakeOutput(acc, delta="", images=list(self.next_images))

    async def close(self):
        pass


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def client_factory(tmp_path, monkeypatch, fake_client):
    """Returns a callable -> fastapi TestClient with the Gemini client faked."""
    from fastapi.testclient import TestClient
    from app import gemini_service

    def make(**overrides):
        values = {
            "cookie_file": str(tmp_path / "cookies.json"),
            "data_dir": str(tmp_path / "data"),
        }
        values.update(overrides)
        app = create_app(Config(values, None))

        async def fake_get_client(self):
            self._client = fake_client
            self._cookie_mode = "anonymous"
            return fake_client

        monkeypatch.setattr(
            gemini_service.GeminiService, "get_client", fake_get_client
        )
        return TestClient(app)

    return make
