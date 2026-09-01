"""Cookie import handling (SRS 2.2).

Accepts, by shape detection (the caller never says which form it is):
  * a JSON array of cookie objects: ``[{"name": ..., "value": ...}, ...]``
  * a JSON object wrapping a raw header string: ``{"cookie": "a=1; b=2"}``
  * a raw cookie-header string: ``a=1; b=2``

The whole ``google.com`` cookie set is kept, not just the two that matter, because
the session-refresh path touches ``accounts.google.com`` (SRS 2.2).

File reads are cached keyed by (path, mtime, size) so a fresh export is picked up on
the very next read without a fixed poll interval.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("gemini_proxy.cookies")

# The two that carry the session (SRS 2.2). Names are Google's, taken from
# gemini_webapi's own interface, not guessed from another project.
LOGIN_COOKIE = "__Secure-1PSID"
TOKEN_COOKIE = "__Secure-1PSIDTS"


def parse_cookies(payload: str | bytes | list | dict) -> dict[str, str]:
    """Return an ordered ``{name: value}`` mapping from any accepted shape.

    Raises ``ValueError`` if nothing cookie-shaped can be found.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")

    data: object = payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ValueError("empty cookie payload")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _parse_header_string(text)

    if isinstance(data, list):
        return _parse_object_list(data)

    if isinstance(data, dict):
        # {"cookie": "raw string"} or {"cookies": [...]}
        for key in ("cookie", "cookies", "Cookie"):
            if key in data:
                inner = data[key]
                if isinstance(inner, str):
                    return _parse_header_string(inner)
                if isinstance(inner, list):
                    return _parse_object_list(inner)
        # a plain {name: value} mapping
        flat = {
            str(k): str(v)
            for k, v in data.items()
            if isinstance(v, (str, int, float))
        }
        if flat:
            return flat

    raise ValueError("could not detect a cookie shape in payload")


def _parse_header_string(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    if not out:
        raise ValueError("no name=value pairs in cookie header string")
    return out


def _parse_object_list(items: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("Name") or item.get("key")
        value = item.get("value")
        if value is None:
            value = item.get("Value", "")
        if name:
            out[str(name)] = str(value)
    if not out:
        raise ValueError("no usable cookie objects in list")
    return out


class CookieStore:
    """Reads a cookie export file, caching by file-modification signature."""

    def __init__(self, path: str | os.PathLike[str] | None):
        self._path = Path(path).expanduser() if path else None
        self._sig: tuple[float, int] | None = None
        self._cache: dict[str, str] = {}

    @property
    def path(self) -> Path | None:
        return self._path

    def _current_sig(self) -> tuple[float, int] | None:
        if not self._path or not self._path.is_file():
            return None
        st = self._path.stat()
        return (st.st_mtime, st.st_size)

    def load(self) -> dict[str, str]:
        """Return the current cookie mapping, or ``{}`` when no file is configured
        or present (SRS 2.2: no cookie is not a hard error)."""
        sig = self._current_sig()
        if sig is None:
            self._sig, self._cache = None, {}
            return {}
        if sig == self._sig:
            return dict(self._cache)
        try:
            raw = self._path.read_text(encoding="utf-8")  # type: ignore[union-attr]
            parsed = parse_cookies(raw)
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable cookie file %s: %s", self._path, exc)
            self._sig, self._cache = sig, {}
            return {}
        self._sig, self._cache = sig, parsed
        logger.info(
            "Loaded %d cookies from %s (login=%s token=%s)",
            len(parsed),
            self._path,
            LOGIN_COOKIE in parsed,
            TOKEN_COOKIE in parsed,
        )
        return dict(parsed)

    def has_session_cookies(self) -> bool:
        cookies = self.load()
        return LOGIN_COOKIE in cookies
