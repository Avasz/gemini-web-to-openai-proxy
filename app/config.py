"""Configuration loading (SRS 2.1).

Discovery order for the JSON config file:
  1. explicit path passed to load_config()
  2. $GEMINI_PROXY_CONFIG naming a path
  3. ./config.json in the current directory
  4. $XDG_CONFIG_HOME/gemini-openai-proxy/config.json
     (falling back to ~/.config/gemini-openai-proxy/config.json)

A full set of defaults lives in DEFAULTS so the service starts with no config at all.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("gemini_proxy.config")

CONFIG_ENV_VAR = "GEMINI_PROXY_CONFIG"
APP_DIR_NAME = "gemini-openai-proxy"

DEFAULTS: dict[str, Any] = {
    # Network
    "host": "127.0.0.1",
    "port": 8000,
    # Model
    "default_model": "gemini-flash",
    # Auth for generation endpoints; empty list => open (warned at startup)
    "api_keys": [],
    # Cookie source for the Gemini Web session
    "cookie_file": "cookies.json",
    # Gemini "temporary chat" (not saved to account history) default
    "temporary_chat_default": False,
    # Force anonymous/guest tier: ignore the cookie file AND the library's own
    # rotated-cookie cache, and disable auto-refresh. Useful for verifying the
    # credential-free path without a stale cached session shadowing it (SRS 7).
    "force_anonymous": False,
    # Where gemini_webapi keeps its rotated-cookie cache. null => {data_dir}/gemini_webapi
    # (or a pre-set $GEMINI_COOKIE_PATH). Point two instances of THE SAME account at
    # one dir only if at most one of them has auto_refresh on -- two refreshers race
    # each other's __Secure-1PSIDTS rotation.
    "cookie_cache_dir": None,
    # Let the library keep the session's cookies/token fresh in the background.
    # Turn OFF only when another process already owns refresh for this account.
    "auto_refresh": True,
    # Reliability tunables (SRS 2.8)
    "max_concurrent_generations": 3,
    # How long a request waits for a generation slot before a 503.
    "slot_wait_timeout": 60.0,
    # Hard ceiling on a single non-streaming generation (outer asyncio timeout).
    "request_timeout": 180.0,
    "connection_timeout": 60.0,
    "zombie_stream_timeout": 90.0,
    "cookie_refresh_interval": 600.0,
    # Local activity-log retention, in days (SRS 2.7)
    "activity_log_retention_days": 7,
    # Warm reusable chat sessions (SRS 2.11)
    "warm_session_idle_timeout": 900.0,
    "max_warm_sessions": 20,
    # Admin dashboard (SRS 2.9)
    "admin_username": "admin",
    # Auto-rebuild the client when the cookie file's __Secure-1PSID changes
    # (a new session pasted in). 0 disables the watcher.
    "cookie_watch_interval": 15.0,
    # Optional extra file to mirror into cookie_file when it changes (SRS 2.9)
    "cookie_watch_file": None,
    # Image input (SRS 2.5)
    "image_fetch_timeout": 20.0,
    "max_image_bytes": 20971520,
    # Directory for local state (activity db, admin credential, library cookie cache)
    "data_dir": "data",
}


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config"


def _candidate_paths(explicit: str | os.PathLike[str] | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.cwd() / "config.json")
    candidates.append(_xdg_config_home() / APP_DIR_NAME / "config.json")
    return candidates


class Config:
    """Resolved configuration. Access values as attributes."""

    def __init__(self, values: dict[str, Any], source_path: Path | None):
        merged = {**DEFAULTS, **(values or {})}
        self._values = merged
        self.source_path = source_path
        for key, value in merged.items():
            setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def resolve_path(self, value: str | os.PathLike[str]) -> Path:
        """Resolve a possibly-relative path from config against the config file's
        directory when it has one, else the current working directory."""
        p = Path(value).expanduser()
        if p.is_absolute():
            return p
        base = self.source_path.parent if self.source_path else Path.cwd()
        return (base / p).resolve()


def load_config(explicit_path: str | os.PathLike[str] | None = None) -> Config:
    for path in _candidate_paths(explicit_path):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"Failed to read config file {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"Config file {path} must contain a JSON object")
            logger.info("Loaded config from %s", path)
            return Config(data, path)
    logger.info("No config file found; using built-in defaults")
    return Config({}, None)
