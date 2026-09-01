"""Verify a cookie export before pointing the proxy at it.

Loads a cookie file (any of the formats the proxy accepts), authenticates with
``gemini_webapi`` in an isolated cache dir, and reports:

  * the __Secure-1PSID fingerprint (so you can tell two exports apart)
  * which cookie group actually authenticated
  * the resolved account status
  * per-model availability (a real authenticated session shows the non-default
    models as available; a guest/unauthenticated one does not)
  * a one-line verdict

Usage:
    python scripts/check_cookies.py [path/to/cookies.json]   # default: ./cookies.json
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

# import the proxy's own permissive parser
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.cookies import parse_cookies  # noqa: E402


async def _run(path: Path) -> int:
    if not path.is_file():
        print(f"no such file: {path}")
        return 2
    try:
        cookies = parse_cookies(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"could not parse {path}: {exc}")
        return 2

    psid = cookies.get("__Secure-1PSID")
    psidts = cookies.get("__Secure-1PSIDTS")
    print(f"file            : {path}")
    print(f"cookies parsed  : {len(cookies)}  ({', '.join(sorted(cookies))})")
    if not psid:
        print("verdict         : NO __Secure-1PSID -> this export cannot authenticate")
        return 1
    print(f"__Secure-1PSID  : {psid[:16]}…{psid[-6:]}  (sha8 {hashlib.sha256(psid.encode()).hexdigest()[:8]})")
    print(f"__Secure-1PSIDTS: {'present' if psidts else 'MISSING'}")

    isolated = tempfile.mkdtemp(prefix="gemini-cookie-check-")
    os.environ["GEMINI_COOKIE_PATH"] = isolated

    from curl_cffi.requests import Cookies
    from gemini_webapi import GeminiClient

    client = GeminiClient(psid, psidts)
    jar = Cookies()
    for name, value in cookies.items():
        jar.set(name, value, domain=".google.com", secure=True)
    client._cookies = jar

    try:
        await client.init(timeout=60, auto_refresh=False)
    except Exception as exc:  # noqa: BLE001
        print(f"init            : FAILED - {type(exc).__name__}: {exc}")
        print("verdict         : cookies rejected outright")
        return 1

    status = getattr(client, "account_status", "?")
    source = getattr(client, "_cookie_source", "?")
    models = client.list_models() or []
    usable = [m.model_name for m in models if m.is_available]
    blocked = [m.model_name for m in models if not m.is_available]

    print(f"cookie group    : {source}")
    print(f"account status  : {status}")
    print(f"models usable   : {', '.join(usable) or '(none)'}")
    print(f"models blocked  : {', '.join(blocked) or '(none)'}")

    rc = 0
    if blocked and not usable:
        print("verdict         : NOT AUTHENTICATED - no models usable")
        rc = 1
    elif blocked:
        print(
            "verdict         : GUEST / UNAUTHENTICATED - only the default model works. "
            "This export is not a full account session (wrong account, unprovisioned "
            "account, or device-bound export)."
        )
        rc = 1
    else:
        print("verdict         : OK - authenticated, all models available")

    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass
    return rc


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cookies.json")
    sys.exit(asyncio.run(_run(path.expanduser())))


if __name__ == "__main__":
    main()
