"""Per-request option resolution shared by every wire format.

Some knobs need to be set by clients that cannot add fields to the request body
(pi, opencode and other OpenAI-compatible harnesses only let you pin static
*headers* per provider). Those clients set a header; a plain OpenAI client hitting
the same proxy is unaffected.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

# Header a harness pins in its provider config to keep its chats out of the
# Gemini web history. Any of 1/true/yes/on -> temporary; 0/false/no/off -> force
# a persistent chat even when the config default is temporary.
TEMPORARY_HEADER = "X-Gemini-Temporary"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _header_bool(request: Request, name: str) -> bool | None:
    raw = request.headers.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def resolve_temporary(request: Request, body_value: Any, cfg: Any) -> bool:
    """Resolve the effective ``temporary`` flag.

    Precedence: the ``X-Gemini-Temporary`` header (deliberate per-provider knob)
    wins when present and parseable, then an explicit body field, then the
    ``temporary_chat_default`` config value.
    """
    header = _header_bool(request, TEMPORARY_HEADER)
    if header is not None:
        return header
    if body_value is not None:
        return bool(body_value)
    return bool(getattr(cfg, "temporary_chat_default", False))
