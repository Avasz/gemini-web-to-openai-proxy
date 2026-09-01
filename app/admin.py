"""Admin / recovery dashboard (SRS 2.9).

A browser page (behind its own credential, separate from the generation
``api_keys``) that shows the health signals in human-readable form and accepts a
pasted cookie export, applying it immediately with no process restart.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .admin_auth import require_admin
from .cookie_admin import apply_cookie_payload
from .health import build_health

logger = logging.getLogger("gemini_proxy.admin")

router = APIRouter(tags=["admin"])


def _service(request: Request):
    return request.app.state.gemini


@router.get("/admin/status.json", dependencies=[Depends(require_admin)])
async def admin_status(request: Request) -> dict[str, Any]:
    """Authenticated machine-readable status (same data as GET /status, but
    behind the admin credential for monitoring tools that want it gated)."""
    gemini = _service(request)
    activity = request.app.state.activity
    try:
        await gemini.get_client()
    except Exception:  # noqa: BLE001
        pass
    return {
        "health": await build_health(gemini, activity),
        "gemini": await gemini.status_snapshot(),
        "activity": await activity.summary(24.0),
    }


@router.post("/admin/cookies", dependencies=[Depends(require_admin)])
async def admin_apply_cookies(request: Request) -> JSONResponse:
    gemini = _service(request)
    cookie_store = request.app.state.cookie_store

    raw: str | None = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
            raw = body.get("cookies") if isinstance(body, dict) else None
            if raw is None and isinstance(body, (list, dict)):
                raw = json.dumps(body)
        except Exception:  # noqa: BLE001
            raw = None
    else:
        form = await request.form()
        raw = form.get("cookies") if form else None
    if not raw:
        raw = (await request.body()).decode("utf-8", "ignore") or None

    if not raw or not raw.strip():
        return JSONResponse(status_code=400, content={"error": "no cookie payload provided"})

    try:
        result = await apply_cookie_payload(gemini, cookie_store, raw)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    code = 200 if result.get("reinit_ok", True) else 502
    return JSONResponse(status_code=code, content=result)


def _badge(value: Any) -> str:
    if value is True:
        return '<span class="b ok">yes</span>'
    if value is False:
        return '<span class="b bad">no</span>'
    if value is None:
        return '<span class="b unk">n/a</span>'
    cls = "ok" if value in ("ok", "AVAILABLE") else "bad" if value in ("down",) else "warn"
    return f'<span class="b {cls}">{html.escape(str(value))}</span>'


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gemini-openai-proxy admin</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
 h1{{font-size:1.2rem}} h2{{font-size:1rem;margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}}
 table{{border-collapse:collapse;width:100%}} td{{padding:.3rem .5rem;border-bottom:1px solid #eee}}
 td:first-child{{color:#666;width:14rem}}
 .b{{padding:.05rem .5rem;border-radius:1rem;font-size:.85em;font-weight:600}}
 .ok{{background:#d7f5dd;color:#0a6d2a}} .bad{{background:#fbdcdc;color:#a11}}
 .warn{{background:#fdf0d0;color:#8a6d00}} .unk{{background:#eee;color:#666}}
 textarea{{width:100%;height:9rem;font-family:ui-monospace,monospace;font-size:12px}}
 button{{padding:.5rem 1rem;font-size:14px;cursor:pointer}}
 #out{{margin-top:.8rem;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px}}
 .muted{{color:#888}}
</style></head><body>
<h1>gemini-openai-proxy &mdash; admin</h1>

<h2>Health</h2>
<table>
 <tr><td>overall</td><td>{overall}</td></tr>
 <tr><td>page reachable</td><td>{page}</td></tr>
 <tr><td>client authenticated</td><td>{authed}</td></tr>
 <tr><td>account status</td><td>{acct}</td></tr>
 <tr><td>recent requests ok (1h)</td><td>{recent}</td></tr>
 <tr><td>cookie mode</td><td class="muted">{cookie_mode}</td></tr>
 <tr><td>cookie source</td><td class="muted">{cookie_source}</td></tr>
 <tr><td>init error</td><td class="muted">{init_error}</td></tr>
</table>

<h2>Activity (24h)</h2>
<table>
 <tr><td>total / ok / errors</td><td>{a_total} / {a_ok} / {a_err}</td></tr>
 <tr><td>error rate</td><td>{a_rate}</td></tr>
 <tr><td>avg latency</td><td>{a_lat} ms</td></tr>
 <tr><td>since last request</td><td>{a_since}</td></tr>
 <tr><td>per model</td><td class="muted">{a_models}</td></tr>
 <tr><td>errors by code</td><td class="muted">{a_codes}</td></tr>
</table>

<h2>Recover: import cookies</h2>
<p class="muted">Paste a cookie export (JSON array, <code>{{"cookie":"..."}}</code>,
or a raw <code>name=value; ...</code> string). Applied immediately &mdash; the
client is torn down and rebuilt, no restart.</p>
<textarea id="cookies" placeholder="[ {{&quot;name&quot;: &quot;__Secure-1PSID&quot;, &quot;value&quot;: &quot;...&quot;}}, ... ]"></textarea>
<div style="margin-top:.5rem"><button onclick="apply()">Apply cookies</button></div>
<div id="out"></div>

<script>
async function apply(){{
  const out=document.getElementById('out'); out.textContent='applying...';
  try{{
    const r=await fetch('admin/cookies',{{method:'POST',
      headers:{{'content-type':'application/json'}},
      body:JSON.stringify({{cookies:document.getElementById('cookies').value}})}});
    const j=await r.json();
    out.textContent=(r.ok?'OK\\n':'FAILED ('+r.status+')\\n')+JSON.stringify(j,null,2);
  }}catch(e){{ out.textContent='error: '+e; }}
}}
</script>
</body></html>"""


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_page(request: Request) -> HTMLResponse:
    gemini = _service(request)
    activity = request.app.state.activity
    try:
        await gemini.get_client()
    except Exception:  # noqa: BLE001
        pass
    h = await build_health(gemini, activity)
    snap = await gemini.status_snapshot()
    a = await activity.summary(24.0)

    def esc(v: Any) -> str:
        return html.escape(str(v)) if v is not None else "&mdash;"

    page = _PAGE.format(
        overall=_badge(h["overall"]),
        page=_badge(h["page_reachable"]),
        authed=_badge(h["client_authenticated"]),
        acct=_badge(h["account_status"]),
        recent=_badge(h["recent_requests_ok"]),
        cookie_mode=esc(snap.get("cookie_mode")),
        cookie_source=esc(snap.get("cookie_source")),
        init_error=esc(snap.get("init_error")),
        a_total=a.get("total", 0), a_ok=a.get("ok", 0), a_err=a.get("errors", 0),
        a_rate=esc(a.get("error_rate")),
        a_lat=esc(a.get("avg_latency_ms")),
        a_since=esc(a.get("seconds_since_last")),
        a_models=esc(json.dumps(a.get("per_model", {}))),
        a_codes=esc(json.dumps(a.get("errors_by_code", {}))),
    )
    return HTMLResponse(page)
