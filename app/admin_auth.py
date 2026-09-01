"""Admin credential resolution + auth dependency (SRS 2.9).

Kept entirely separate from the generation ``api_keys`` (SRS 2.9): a generation
key grants no admin access and vice versa.

Credential resolution, in order:
  1. ``ADMIN_PASSWORD`` in the environment / ``.env`` (pinned by the operator)
  2. ``{data_dir}/admin_credential`` file, if it exists
  3. otherwise: generate a random one, persist it 0600, and log it prominently

Accepted on a request as any of:
  * HTTP Basic ``Authorization: Basic base64(user:pass)``  (browser prompt)
  * ``X-Admin-Password`` / ``X-Admin-Key`` header
  * ``?admin_key=`` / ``?admin_password=`` query parameter
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import secrets
import stat
from pathlib import Path

from fastapi import HTTPException, Request, Response, status

_COOKIE = "gop_admin"

logger = logging.getLogger("gemini_proxy.admin")

_ENV_VAR = "ADMIN_PASSWORD"
_CRED_FILENAME = "admin_credential"


class AdminCredential:
    def __init__(self, username: str, password: str, source: str):
        self.username = username
        self.password = password
        self.source = source

    def check(self, username: str | None, password: str | None) -> bool:
        u_ok = username is None or hmac.compare_digest(username, self.username)
        p_ok = password is not None and hmac.compare_digest(password, self.password)
        return bool(u_ok and p_ok)


def resolve_admin_credential(data_dir: Path, username: str = "admin") -> AdminCredential:
    env_pw = os.environ.get(_ENV_VAR, "").strip()
    if env_pw:
        logger.info("Admin credential: using %s from environment.", _ENV_VAR)
        return AdminCredential(username, env_pw, "env")

    cred_file = data_dir / _CRED_FILENAME
    if cred_file.is_file():
        pw = cred_file.read_text(encoding="utf-8").strip()
        if pw:
            logger.info("Admin credential: loaded from %s", cred_file)
            return AdminCredential(username, pw, "file")

    pw = secrets.token_urlsafe(24)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(pw + "\n", encoding="utf-8")
        cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        source = "generated"
    except OSError as exc:
        logger.warning("Could not persist admin credential to %s: %s", cred_file, exc)
        source = "generated-ephemeral"

    logger.warning(
        "\n"
        "  ┌─────────────────────────────────────────────────────────────────┐\n"
        "  │  ADMIN DASHBOARD CREDENTIAL (generated on first boot)            │\n"
        "  │  username: %-52s│\n"
        "  │  password: %-52s│\n"
        "  │  %-63s│\n"
        "  └─────────────────────────────────────────────────────────────────┘",
        username,
        pw,
        f"stored at {cred_file}" if source == "generated" else "NOT persisted (fs error)",
    )
    return AdminCredential(username, pw, source)


def _basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    user, _, pw = decoded.partition(":")
    return user, pw


async def require_admin(request: Request) -> None:
    """Accepts HTTP Basic, ``X-Admin-Password`` / ``X-Admin-Key`` header,
    ``?admin_key=`` query param, or the ``gop_admin`` session cookie."""
    cred: AdminCredential = request.app.state.admin_credential

    cookie = request.cookies.get(_COOKIE)
    if cookie and cred.check(None, cookie):
        return

    basic = _basic_auth(request.headers.get("authorization"))
    if basic and cred.check(basic[0], basic[1]):
        return

    header_pw = request.headers.get("x-admin-password") or request.headers.get("x-admin-key")
    if header_pw and cred.check(None, header_pw.strip()):
        return

    q = request.query_params
    query_pw = q.get("admin_key") or q.get("admin_password")
    if query_pw and cred.check(None, query_pw.strip()):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required.",
        headers={"WWW-Authenticate": 'Basic realm="gemini-openai-proxy admin"'},
    )


def attach_admin_session(response: Response, request: Request) -> Response:
    """Set the ``gop_admin`` session cookie so the dashboard's XHRs (which send
    no auth header) stay authenticated after the first page load."""
    cred: AdminCredential = request.app.state.admin_credential
    response.set_cookie(
        _COOKIE, cred.password, max_age=86400,
        httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response
