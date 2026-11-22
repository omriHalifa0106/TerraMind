// TerraMind frontend: POST + SSE stream reader, live agent trace, Leaflet map.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { threadId: localStorage.getItem("tm-thread") || null, busy: false, botEl: null, botText: "" };

// ---------- map ----------
const map = L.map("map", { zoomControl: true }).setView([33.0, 35.5], 10);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18, attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);
const chosenLayer = L.layerGroup().addTo(map);
const vetoLayer = L.layerGroup().addTo(map);

async function fetchTrail(trailId) {
  const r = await fetch(`/trails/${encodeURIComponent(trailId)}/geojson`);
  return r.ok ? r.json() : null;
}

async function drawVetoed(trailId, reasons) {
  const feature = await fetchTrail(trailId);
  if (!feature) return;
  L.geoJSON(feature, { style: { color: "#b3412f", weight: 3, dashArray: "6 6", opacity: 0.8 } })
    .bindPopup(`<b>${escapeHtml(feature.properties.name_he || feature.properties.name)}</b><br>${reasons.map(escapeHtml).join("<br>")}`)
    .addTo(vetoLayer);
}

async function drawPlan(plan) {
  chosenLayer.clearLayers();
  const feature = await fetchTrail(plan.trail_id);
  const bounds = [];
  if (feature) {
    const layer = L.geoJSON(feature, { style: { color: "#2f5d46", weight: 5, opacity: 0.9 } }).addTo(chosenLayer);
    bounds.push(layer.getBounds());
  }
  plan.stops.forEach((s, i) => {
    const icon = L.divIcon({ className: "", html: `<div class="stop-pin">${i + 1}</div>`, iconSize: [24, 24] });
    const time = s.arrive_at ? s.arrive_at.slice(0, 5) : "";
    L.marker(s.location, { icon }).bindPopup(`<b>${escapeHtml(s.name)}</b><br>${time}${s.note ? "<br>" + escapeHtml(s.note) : ""}`)
      .addTo(chosenLayer);
    bounds.push(L.latLngBounds([s.location, s.location]));
  });
  if (bounds.length) map.fitBounds(bounds.reduce((a, b) => a.extend(b)), { padding: [40, 40] });
}

// ---------- chat + trace ----------
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function renderMarkdown(text) {
  return window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(text)) : escapeHtml(text).replace(/\n/g, "<br>");
}
function addMessage(role, html) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = html;
  $("chat").appendChild(el);
  $("chat").scrollTop = $("chat").scrollHeight;
  return el;
}
const NODE_LABELS = {
  load_profile: "טוען העדפות שמורות", supervisor: "מנתב את הבקשה", intake: "מחלץ פרמטרים",
  researcher: "מחפש מסלולים (מרחבי + סמנטי)", grader: "מדרג רלוונטיות", planner: "בונה מסלול",
  enrich: "בודק מזג אוויר, אור יום והתראות", safety_critic: "בדיקת בטיחות", safe_fallback: "אין חלופה בטוחה",
  explainer: "מנסח הסבר", save_memory: "שומר העדפות",
};
function trace(tag, text, cls = "") {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<span class="tag">${escapeHtml(tag)}</span><span class="txt">${escapeHtml(text)}</span>`;
  $("trace").appendChild(li);
  $("trace-count").textContent = `(${$("trace").children.length})`;
  li.scrollIntoView({ block: "nearest" });
}

function handleEvent(type, data) {
  switch (type) {
    case "start":
      state.threadId = data.thread_id;
      localStorage.setItem("tm-thread", data.thread_id);
      break;
    case "node":
      if (data.status === "start") {
        const extra = data.node === "researcher" ? ` · סבב ${data.round}${data.relaxed ? " (מסננים מורחבים)" : ""}` : "";
        trace(data.node, (NODE_LABELS[data.node] || data.node) + extra);
      }
      break;
    case "route": trace("route", `כוונה: ${data.intent}`); break;
    case "candidates":
      trace("candidates", data.trails.length ? data.trails.map((t) => `${t.name} (${t.km} ק"מ)`).join(" · ") : "לא נמצאו מועמדים");
      break;
    case "graded": trace("grader", `נשמרו ${data.kept} מתוך ${data.total} קטעי ידע`); break;
    case "tool_calls": if (data.calls.length) trace("tools", data.calls.join(", ")); break;
    case "plan": trace("plan", `${data.plan.title} · ${data.plan.start_time.slice(0, 5)}-${data.plan.end_time.slice(0, 5)}`); break;
    case "conditions": {
      const w = data.weather;
      const wt = w && w.status !== "unavailable" && w.status !== "out_of_range"
        ? `${w.temp_max_c ?? "?"}°C · גשם ${w.precip_prob_max_pct ?? "?"}% · שקיעה ${w.sunset ? w.sunset.slice(11, 16) : "?"}`
        : `תחזית: ${w ? w.status : "לא זמינה"}`;
      const st = data.status ? (data.status.is_open ? "פתוח" : "סגור") : "?";
      trace("enrich", `${wt} · מסלול ${st} · התראות: ${data.alerts.length}`);
      break;
    }
    case "verdict": {
      const crit = data.issues.filter((i) => i.severity === "critical");
      if (data.approved) trace("safety", `אושר (ניסיון ${data.attempt})${data.issues.length ? " · אזהרות: " + data.issues.length : ""}`, data.issues.length ? "warn" : "");
      else {
        trace("veto", `וטו (${data.scope === "timing" ? "שינוי שעה" : "מסלול חלופי"}): ${crit.map((i) => i.evidence).join("; ")}`, "veto");
        if (data.scope === "trail" && crit.length) drawVetoed(crit[0].trail_id, crit.map((i) => i.evidence));
      }
      break;
    }
    case "token":
      if (!state.botEl) { state.botEl = addMessage("bot streaming", ""); state.botText = ""; }
      state.botText += data.text;
      state.botEl.innerHTML = renderMarkdown(state.botText);
      $("chat").scrollTop = $("chat").scrollHeight;
      break;
    case "final":
      if (state.botEl) state.botEl.classList.remove("streaming");
      if (data.final.plan) drawPlan(data.final.plan);
      break;
    case "message": addMessage("bot", renderMarkdown(data.text)); break;
    case "warning": trace("warn", data.detail, "warn"); break;
    case "error": addMessage("error", escapeHtml(data.detail)); break;
    case "done":
      if (data.tool_calls) trace("done", `${data.tool_calls.length} קריאות כלים · ${data.tool_calls.reduce((a, c) => a + c.ms, 0)}ms`);
      break;
  }
}

// EventSource cannot POST, so we read the SSE stream from fetch() ourselves.
async function send(message) {
  state.busy = true; $("send").disabled = true; state.botEl = null;
  addMessage("user", escapeHtml(message));
  $("trace").innerHTML = ""; vetoLayer.clearLayers();
  try {
    await syncScenario();
    const resp = await fetch("/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, thread_id: state.threadId, user_id: "demo-user" }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value.replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
        let type = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) { try { handleEvent(type, JSON.parse(data)); } catch (e) { console.warn("bad event", e, data); } }
      }
    }
  } catch (err) {
    addMessage("error", escapeHtml(`שגיאה: ${err.message}`));
  } finally {
    state.busy = false; $("send").disabled = false;
    if (state.botEl) state.botEl.classList.remove("streaming");
  }
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (text && !state.busy) { $("input").value = ""; send(text); }
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});

// ---------- scenarios + health ----------
// The mock API keeps its scenario in memory (it resets to "normal" when restarted), so the
// dropdown is the source of truth: re-send it before every message and confirm what the server has.
async function syncScenario() {
  const sel = $("scenario");
  if (sel.disabled || !sel.value) return;
  try {
    const r = await fetch(`/scenarios/${encodeURIComponent(sel.value)}`, { method: "POST" });
    const active = r.ok ? (await r.json()).scenario : null;
    trace("scenario", active ? `תרחיש פעיל: ${active}` : "לא הצלחתי לקבוע תרחיש", active ? "" : "warn");
  } catch { trace("scenario", "שרת התרחישים לא זמין", "warn"); }
}

async function initScenarios() {
  try {
    const r = await fetch("/scenarios");
    const { scenarios, current } = await r.json();
    const sel = $("scenario");
    for (const [name, desc] of Object.entries(scenarios)) {
      const opt = new Option(name, name, false, name === current);
      opt.title = desc; sel.add(opt);
    }
    sel.addEventListener("change", () => fetch(`/scenarios/${sel.value}`, { method: "POST" }));
  } catch { $("scenario").disabled = true; }
}
async function initHealth() {
  try {
    const h = await (await fetch("/health")).json();
    const ok = h.mcp_servers.length === 4;
    $("health").textContent = `MCP ${h.mcp_servers.length}/4 · ${h.llm}`;
    $("health").classList.toggle("bad", !ok);
  } catch { $("health").textContent = "API offline"; $("health").classList.add("bad"); }
}
addMessage("bot", "שלום! ספרו לי איפה, מתי ועם מי תרצו לטייל, ואבנה לכם מסלול עם בדיקת בטיחות והסבר.");
initScenarios(); initHealth();
