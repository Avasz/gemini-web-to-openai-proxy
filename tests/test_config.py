import json

from app.config import DEFAULTS, load_config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_PROXY_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config()
    assert cfg.source_path is None
    assert cfg.port == DEFAULTS["port"]
    assert cfg.max_concurrent_generations == 3


def test_explicit_path_overrides_and_merges(tmp_path):
    p = tmp_path / "custom.json"
    p.write_text(json.dumps({"port": 9999, "api_keys": ["abc"]}))
    cfg = load_config(p)
    assert cfg.port == 9999
    assert cfg.api_keys == ["abc"]
    # unspecified keys fall back to defaults
    assert cfg.default_model == DEFAULTS["default_model"]


def test_env_var_path(tmp_path, monkeypatch):
    p = tmp_path / "viaenv.json"
    p.write_text(json.dumps({"host": "0.0.0.0"}))
    monkeypatch.setenv("GEMINI_PROXY_CONFIG", str(p))
    cfg = load_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.source_path == p


def test_cwd_config_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_PROXY_CONFIG", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"port": 1234}))
    cfg = load_config()
    assert cfg.port == 1234


def test_relative_path_resolves_against_config_dir(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"cookie_file": "cookies.json"}))
    cfg = load_config(p)
    assert cfg.resolve_path(cfg.cookie_file) == (tmp_path / "cookies.json").resolve()
