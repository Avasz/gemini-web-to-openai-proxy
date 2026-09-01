import json

from app.config import Config
from app.cookies import CookieStore
from app.gemini_service import GeminiService


def _svc(tmp_path, **overrides):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"__Secure-1PSID": "abc", "extra": "1"}))
    values = {"data_dir": str(tmp_path / "data"), "cookie_file": str(cookie_file)}
    values.update(overrides)
    cfg = Config(values, None)
    return GeminiService(cfg, CookieStore(cookie_file))


def test_build_client_passes_full_cookie_set(tmp_path):
    svc = _svc(tmp_path)
    client, mode = svc._build_client()
    assert mode == "authenticated"
    jar = {c.name for c in client.cookies.jar}
    assert {"__Secure-1PSID", "extra"} <= jar


def test_force_anonymous_ignores_cookies(tmp_path):
    svc = _svc(tmp_path, force_anonymous=True)
    client, mode = svc._build_client()
    assert mode == "anonymous(forced)"
    assert list(client.cookies.jar) == []
    assert svc._cookie_cache_dir.name == "gemini_webapi_anon"


def test_cache_dir_env_is_set_under_data_dir(tmp_path):
    import os

    svc = _svc(tmp_path)
    assert os.environ["GEMINI_COOKIE_PATH"] == str(svc._cookie_cache_dir)
    assert svc._cookie_cache_dir.is_dir()
