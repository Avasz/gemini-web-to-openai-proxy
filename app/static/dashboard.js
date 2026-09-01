"use strict";
const $ = (id) => document.getElementById(id);
const REFRESH_MS = 30000;

/* ─── theme ──────────────────────────────────────────────────────── */
(function () {
  try {
    const t = localStorage.getItem("gop-theme");
    if (t) document.documentElement.setAttribute("data-theme", t);
    else if (matchMedia("(prefers-color-scheme: light)").matches)
      document.documentElement.setAttribute("data-theme", "light");
  } catch (e) {}
})();
function toggleTheme() {
  const next =
    document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("gop-theme", next); } catch (e) {}
}
["theme-side", "theme-mobile"].forEach((id) => { const e = $(id); if (e) e.onclick = toggleTheme; });

/* ─── helpers ────────────────────────────────────────────────────── */
function setPill(el, value, cls) {
  el.textContent = value;
  el.className = "pill" + (cls ? " " + cls : "");
}
function boolPill(el, v, t = "Yes", f = "No") {
  setPill(el, v === true ? t : v === false ? f : "n/a", v === true ? "ok" : v === false ? "danger" : "warn");
}
const fmtSecs = (s) =>
  s == null ? "–" : s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : Math.round(s / 3600) + "h";
function fmtUptime(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return (d ? d + "d " : "") + p(h) + ":" + p(m) + ":" + p(sec);
}
// fix (round 2 #1): usage is NOT green-at-low. neutral until it's worth flagging.
const usageLevel = (pct) => (pct == null ? "neutral" : pct < 60 ? "neutral" : pct < 85 ? "warn" : "danger");

let uptimeBase = null, uptimeAt = 0;

function renderUsage(usage, tierLabel, health) {
  const card = $("usage-card"), body = $("usage-body");
  card.hidden = false;                       // always visible
  $("usage-tier").hidden = !tierLabel;
  if (tierLabel) $("usage-tier").textContent = tierLabel;

  const windows = [];
  if (usage) {
    if (usage.current_5h) windows.push(["5-Hour Window", usage.current_5h]);
    if (usage.weekly) windows.push(["Weekly Window", usage.weekly]);
  }
  if (!windows.length) {
    const why =
      health && health.client_authenticated === false
        ? "The client is not authenticated — quota is only reported for an authenticated account."
        : "No quota windows reported yet.";
    body.innerHTML = `<div class="usage"><div class="usage-empty">${why}</div></div>`;
    return;
  }

  body.innerHTML = windows.map(([name, w]) => {
    const pct = w.usage_percentage ?? 0;
    const lv = usageLevel(pct);
    const rem = w.remaining_credits, ul = w.usage_level;
    // fix (round 2 #2): show consumed / total, not just remaining. total is
    // back-derived: remaining is (1 - usage_level) of the pool.
    let foot = "";
    if (rem != null && ul != null && ul < 1) {
      const total = Math.round(rem / (1 - ul));
      foot = `${(total - rem).toLocaleString()} / ${total.toLocaleString()} credits used`;
    } else if (rem != null) {
      foot = `${rem.toLocaleString()} credits remaining`;
    }
    const reset = w.reset_at ? "Resets " + new Date(w.reset_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
    return `<div class="usage">
      <div class="usage-top"><span class="usage-name">${name}</span><span class="usage-pct" data-lv="${lv}">${pct}%</span></div>
      <div class="usage-track"><div class="usage-fill" data-lv="${lv}" style="width:${Math.min(pct, 100)}%"></div></div>
      <div class="usage-foot"><span>${foot}</span><span>${reset}</span></div></div>`;
  }).join("");
}

function renderInner(d) {
  $("version").textContent = "v" + (d.version || "–");
  uptimeBase = d.uptime_seconds; uptimeAt = Date.now();
  $("uptime").textContent = fmtUptime(uptimeBase);

  const h = d.health || {}, g = d.gemini || {};
  const b = $("banner");
  b.dataset.status = h.overall || "unknown";
  $("banner-ok").hidden = h.overall !== "ok";
  $("banner-bad").hidden = h.overall === "ok";
  $("banner-title").textContent =
    h.overall === "ok" ? "All Systems Operational"
    : h.overall === "degraded" ? "Degraded" : h.overall === "down" ? "Down" : "Unknown";
  $("banner-text").textContent =
    h.overall === "ok" ? "Service healthy · authenticated · requests flowing"
    : h.client_authenticated === false ? "Client is not authenticated — import fresh cookies"
    : h.page_reachable === false ? "gemini.google.com not reachable with this cookie"
    : "See Account & Auth below";

  const authed = h.client_authenticated === true;
  setPill($("auth-pill"), authed ? "Valid" : "Degraded", authed ? "ok" : "danger");
  const mode = g.cookie_mode || "unknown";
  setPill($("s-mode"),
    mode.startsWith("authenticated") ? "cookie_valid" : mode.startsWith("anonymous") ? "anonymous" : mode,
    mode.startsWith("authenticated") ? "ok" : "warn");
  boolPill($("s-auth"), h.client_authenticated);
  boolPill($("s-init"), g.ready);
  setPill($("s-acct"), h.account_status || "n/a", h.account_status === "AVAILABLE" ? "ok" : h.account_status ? "danger" : "warn");
  boolPill($("s-page"), h.page_reachable);
  $("s-source").textContent = g.cookie_source || "n/a";
  $("s-detail").textContent = g.init_error
    ? g.init_error
    : authed ? "Cookie accepted; account is authenticated."
    : "Page loads but the account is not authenticated — the client is serving guest tier.";

  const tier = d.usage && d.usage.tier && d.usage.tier.label;
  $("tier").hidden = !tier;
  if (tier) $("tier").textContent = tier;

  $("m-default").textContent = d.default_model || "–";
  $("m-tier").innerHTML = tier ? `<span class="badge badge-accent">${tier}</span>` : '<span class="kv-dim">unknown</span>';
  $("m-keys").textContent = d.api_keys_required ? "required" : "open";
  $("m-list").innerHTML = (d.models && d.models.length)
    ? d.models.map((m) => `<span class="tag${m.is_available ? "" : " off"}">${m.display_name || m.name}</span>`).join("")
    : '<span class="kv-dim">not initialized — import cookies</span>';

  const a = d.activity || {};
  $("a-total").textContent = a.total ?? "–";
  $("a-errors").textContent = a.errors ?? "–";
  $("a-rate").textContent = a.error_rate != null ? (a.error_rate * 100).toFixed(1) + "% error rate" : "";
  $("a-latency").innerHTML = a.avg_latency_ms != null ? `${Math.round(a.avg_latency_ms)}<span class="unit">ms</span>` : "–";
  $("a-last").innerHTML = a.seconds_since_last != null ? `${Math.round(a.seconds_since_last)}<span class="unit">s</span>` : "–";
  $("a-bymodel").innerHTML = a.per_model && Object.keys(a.per_model).length
    ? Object.entries(a.per_model).map(([m, s]) => `<span class="breakdown-chip">${m} · <strong>${s.count}</strong></span>`).join(" ")
    : '<span class="breakdown-chip kv-dim">no data yet</span>';

  const w = d.warm_sessions, c = d.capacity || {};
  setPill($("w-count"), w ? `${w.active} Session${w.active === 1 ? "" : "s"}` : "n/a", "ok");
  $("w-inflight").textContent = c.limit != null ? `${c.in_flight} / ${c.limit}` + (c.waiting ? ` (+${c.waiting} waiting)` : "") : "–";
  $("w-idle").textContent = w ? `${w.idle_timeout}s (${Math.round(w.idle_timeout / 60)}m)` : "–";
  const inbox = d.cookie_inbox || {};
  $("w-inbox").textContent = inbox.path || "not configured";
  $("ctx-path").textContent = inbox.path || "not configured";
  const lastImp = inbox.last_import_at
    ? `${Math.round((Date.now() / 1000 - inbox.last_import_at))}s ago (${inbox.last_import_count} cookies)`
    : "Never";
  $("w-lastimport").textContent = lastImp;
  $("ctx-last").textContent = lastImp;

  renderUsage(d.usage, tier, h);
}
function render(d) {
  try { renderInner(d); } catch (e) { console.error("render failed", e); }
}

/* ─── polling + refresh bar ──────────────────────────────────────── */
let editing = false, elapsed = 0;
async function poll() {
  elapsed = 0;
  try {
    const r = await fetch("/admin/status.json", { headers: { Accept: "application/json" } });
    if (r.status === 401) { location.href = "/admin"; return; }
    render(await r.json());
  } catch (e) { /* keep last render */ }
}
setInterval(() => {
  // live-tick the uptime clock
  if (uptimeBase != null) $("uptime").textContent = fmtUptime(uptimeBase + Math.floor((Date.now() - uptimeAt) / 1000));
  if (editing) { $("pause-notice").classList.add("visible"); $("refresh-fill").style.width = "0%"; return; }
  $("pause-notice").classList.remove("visible");
  elapsed += 1000;
  $("refresh-fill").style.width = Math.min((elapsed / REFRESH_MS) * 100, 100) + "%";
  if (elapsed >= REFRESH_MS) poll();
}, 1000);
poll();

/* ─── modal ──────────────────────────────────────────────────────── */
const modal = $("modal"), ta = $("cookie-textarea");
const open = () => { modal.classList.add("open"); editing = true; ta.focus(); };
const close = () => { modal.classList.remove("open"); syncEditing(); };
function syncEditing() {
  editing = modal.classList.contains("open") || ta.value.trim().length > 0 || document.activeElement === ta;
}
["open-import-side", "open-import-auth"].forEach((id) => { const e = $(id); if (e) e.onclick = open; });
$("close-import").onclick = close;
modal.onclick = (e) => { if (e.target === modal) close(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modal.classList.contains("open")) close(); });
["focus", "blur", "input"].forEach((ev) => ta.addEventListener(ev, syncEditing));

$("submit-import").onclick = async () => {
  const text = ta.value.trim();
  if (!text) return;
  const res = $("import-result"), btn = $("submit-import");
  btn.disabled = true;
  res.className = "import-result"; res.textContent = "applying…"; res.classList.add("success");
  try {
    const r = await fetch("/admin/cookies", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ cookies: text }),
    });
    const j = await r.json();
    res.className = "import-result " + (r.ok ? "success" : "error");
    res.textContent = r.ok
      ? `Applied ${j.cookie_count} cookies · ${j.reinit_ok ? "reinitialised (" + (j.cookie_mode || "ok") + ")" : "reinit failed: " + (j.error || "")}`
      : (j.error || j.detail || `Failed (${r.status})`);
    if (r.ok) { ta.value = ""; setTimeout(() => { close(); poll(); }, 1600); }
  } catch (e) {
    res.className = "import-result error"; res.textContent = "Request failed — check your connection.";
  }
  btn.disabled = false;
};
