"use strict";
const $ = (id) => document.getElementById(id);
const REFRESH_MS = 30000;
let editing = false;
let countdown = REFRESH_MS / 1000;

// ---- theme -----------------------------------------------------------------
(function () {
  try {
    const t = localStorage.getItem("gop-theme");
    if (t) document.documentElement.setAttribute("data-theme", t);
    else if (matchMedia("(prefers-color-scheme: light)").matches)
      document.documentElement.setAttribute("data-theme", "light");
  } catch (e) {}
})();
$("theme-toggle").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", cur);
  try { localStorage.setItem("gop-theme", cur); } catch (e) {}
};

// ---- helpers -------------------------------------------------------------
function pill(el, value, opts = {}) {
  el.textContent = value === true ? "yes" : value === false ? "no" : (value ?? "n/a");
  let cls = "";
  if (value === true || value === "ok" || value === "AVAILABLE") cls = "ok";
  else if (value === false || value === "down") cls = "bad";
  else if (value === "degraded" || value == null) cls = "warn";
  else if (typeof value === "string" && value !== "AVAILABLE") cls = "bad";
  el.className = "pill" + (cls ? " " + cls : "");
}
const fmtSecs = (s) =>
  s == null ? "–" : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`;
function fmtUptime(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return (d ? d + "d " : "") + String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
}

function renderUsage(usage) {
  const card = $("usage-card"), body = $("usage-body");
  if (!usage) { card.hidden = true; return; }
  card.hidden = false;
  const windows = [];
  if (usage.current_5h) windows.push(["5-hour window", usage.current_5h]);
  if (usage.weekly) windows.push(["weekly window", usage.weekly]);
  if (!windows.length) { card.hidden = true; return; }  // guest tier: no windows
  body.innerHTML = windows.map(([name, w]) => {
    const pct = w.usage_percentage ?? 0;
    const lv = pct >= 85 ? "bad" : pct >= 60 ? "warn" : "";
    const rem = w.remaining_credits != null ? `${w.remaining_credits.toLocaleString()} left` : "";
    const reset = w.reset_at ? "resets " + new Date(w.reset_at).toLocaleString() : "";
    return `<div class="usage">
      <div class="usage-top"><span class="usage-name">${name}</span><span class="usage-pct">${pct}%</span></div>
      <div class="bar"><i class="${lv}" style="width:${Math.min(pct, 100)}%"></i></div>
      <div class="usage-foot"><span>${rem}</span><span>${reset}</span></div></div>`;
  }).join("");
}

function render(d) {
  $("version").textContent = "v" + (d.version || "–");
  $("uptime").textContent = fmtUptime(d.uptime_seconds);

  const h = d.health || {};
  const b = $("banner");
  b.dataset.status = h.overall || "unknown";
  $("banner-title").textContent =
    h.overall === "ok" ? "Healthy" : h.overall === "degraded" ? "Degraded" : h.overall === "down" ? "Down" : "Unknown";
  $("banner-text").textContent =
    h.overall === "ok" ? "authenticated · requests flowing"
    : h.client_authenticated === false ? "client is not authenticated — import fresh cookies"
    : h.page_reachable === false ? "gemini.google.com not reachable with this cookie"
    : "see account & auth below";

  pill($("s-overall"), h.overall);
  pill($("s-page"), h.page_reachable);
  pill($("s-auth"), h.client_authenticated);
  pill($("s-acct"), h.account_status);
  const g = d.gemini || {};
  $("s-mode").textContent = g.cookie_mode || "–";
  $("s-source").textContent = g.cookie_source || "–";
  $("s-error").textContent = g.init_error || "none";

  const c = d.capacity || {};
  $("c-limit").textContent = c.limit ?? "–";
  $("c-inflight").textContent = c.in_flight ?? "–";
  $("c-waiting").textContent = c.waiting ?? "–";
  $("c-rejected").textContent = c.rejected_total ?? "–";
  const w = d.warm_sessions;
  $("c-warm").textContent = w ? `${w.active} / ${w.max}` : "–";

  const tierLabel = d.usage && d.usage.tier && d.usage.tier.label;
  $("tier").hidden = !tierLabel;
  if (tierLabel) $("tier").textContent = tierLabel;
  renderUsage(d.usage);

  const a = d.activity || {};
  $("a-total").textContent = a.total ?? "–";
  $("a-errors").textContent = a.errors ?? "–";
  $("a-rate").textContent = a.error_rate != null ? (a.error_rate * 100).toFixed(1) + "% rate" : "";
  $("a-latency").textContent = a.avg_latency_ms != null ? Math.round(a.avg_latency_ms) + "ms" : "–";
  $("a-last").textContent = fmtSecs(a.seconds_since_last);
  $("a-bymodel").innerHTML = a.per_model && Object.keys(a.per_model).length
    ? Object.entries(a.per_model).map(([m, s]) => `<span class="chip">${m} · <b>${s.count}</b></span>`).join("")
    : '<span class="dim">no data yet</span>';

  $("model-list").innerHTML = (d.models && d.models.length)
    ? d.models.map((m) =>
        `<span class="chip${m.is_available ? "" : " off"}">${m.display_name || m.name}${
          m.is_available ? "" : " · unavailable"}</span>`).join("")
    : '<span class="dim">not initialized</span>';
}

// ---- polling -----------------------------------------------------------
async function poll() {
  try {
    const r = await fetch("/admin/status.json", { headers: { Accept: "application/json" } });
    if (r.status === 401) { location.reload(); return; }
    render(await r.json());
  } catch (e) { /* keep the last render */ }
}
setInterval(() => {
  if (editing) { $("countdown").textContent = "paused"; return; }
  countdown -= 1;
  if (countdown <= 0) { countdown = REFRESH_MS / 1000; poll(); }
  $("countdown").textContent = countdown;
}, 1000);
poll();

// ---- cookie import modal ----------------------------------------------
const modal = $("import-modal");
const openModal = () => { modal.hidden = false; editing = true; $("cookie-text").focus(); };
const closeModal = () => { modal.hidden = true; editing = false; countdown = REFRESH_MS / 1000; };
$("open-import").onclick = openModal;
$("close-import").onclick = closeModal;
modal.onclick = (e) => { if (e.target === modal) closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });

$("submit-import").onclick = async () => {
  const text = $("cookie-text").value.trim();
  const res = $("import-result");
  if (!text) return;
  const btn = $("submit-import");
  btn.disabled = true;
  res.className = ""; res.textContent = "applying…";
  try {
    const r = await fetch("/admin/cookies", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ cookies: text }),
    });
    const j = await r.json();
    if (r.ok) {
      res.className = "ok";
      res.textContent = `applied ${j.cookie_count} cookies · ${j.cookie_mode || "reinit"}`;
      setTimeout(() => { closeModal(); poll(); }, 1400);
    } else {
      res.className = "bad";
      res.textContent = j.error || j.detail || `failed (${r.status})`;
    }
  } catch (e) {
    res.className = "bad"; res.textContent = "request failed";
  }
  btn.disabled = false;
};
