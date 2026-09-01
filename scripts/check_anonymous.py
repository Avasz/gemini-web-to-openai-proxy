"""Live check: does a zero-credential (anonymous/guest) Gemini session work?

SRS 2.2 + 7: run this from an ISOLATED cache location so a previously cached
authenticated session on this machine can't produce a false positive. This script
points the library's cookie cache at a throwaway temp dir before importing it.

Usage:
    python scripts/check_anonymous.py            # prints a short verdict
    python scripts/check_anonymous.py --prompt "hi"   # also sends one prompt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile


async def _run(send_prompt: str | None) -> int:
    isolated = tempfile.mkdtemp(prefix="gemini-anon-check-")
    # gemini_webapi caches under a per-user dir; redirect HOME/appdirs to isolate it.
    os.environ["HOME"] = isolated
    os.environ["XDG_CACHE_HOME"] = os.path.join(isolated, "cache")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(isolated, "config")
    os.environ["TMPDIR"] = isolated

    from gemini_webapi import GeminiClient

    client = GeminiClient(None, None)  # no cookies at all
    try:
        await client.init(timeout=60, auto_close=False, auto_refresh=False)
    except Exception as exc:  # noqa: BLE001
        print(f"ANON INIT FAILED: {type(exc).__name__}: {exc}")
        return 1

    print("ANON INIT OK")
    print(f"  isolated cache dir : {isolated}")
    print(f"  access_token set   : {bool(client.access_token)}")
    print(f"  cookie source      : {getattr(client, '_cookie_source', '?')}")
    models = client.list_models()
    print(f"  models visible     : {len(models) if models else 0}")

    rc = 0
    if send_prompt:
        try:
            resp = await client.generate_content(send_prompt)
            text = (resp.text or "").strip().replace("\n", " ")
            print(f"  prompt reply       : {text[:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  prompt FAILED      : {type(exc).__name__}: {exc}")
            rc = 1

    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass
    return rc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", help="also send this prompt through the guest session")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.prompt)))


if __name__ == "__main__":
    main()
