"""Minimal .env loader (SRS 2.1).

Rules:
  - `KEY=VALUE` lines; blank lines and `#` comments ignored.
  - surrounding single/double quotes on the value are stripped.
  - a real environment variable already set always wins over a .env value.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str] = ".env") -> dict[str, str]:
    p = Path(path).expanduser()
    applied: dict[str, str] = {}
    if not p.is_file():
        return applied
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
