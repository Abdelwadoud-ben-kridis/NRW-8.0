// app.js — the HMI half of the dashboard (criterion 8, 15 pts).
// No framework, no build step. One WebSocket in, one DOM out.

import { L } from "./labels.js";
import { initTwin, updateTwin, setCamera, toggleFollow, COLOR } from "./twin.js";

const $ = (id) => document.getElementById(id);
const api = (path, body) =>
  fetch("/api" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => r.json()).then((res) => {
    // A refused operation (backend/warehouse.py::OpError -> 400/404/409)
    // still resolves as JSON with an `error` field -- surface it on the
    // banner instead of failing silently, so a double-press or a stale
    // order shows something instead of nothing happening.
    if (res && res.error) {
      const el = $("banner");
      el.className = "quarantine";
      el.textContent = res.error;
      clearTimeout(el._t);
      el._t = setTimeout(() => { el.className = ""; }, 6000);
    }
    return res;
  });

let ST = null;            // latest snapshot
let ARTICLES = [];
let lastBannerT = -1;
let lastOrderId = null;
let firstRender = true;
// True while a confirm/cancel round trip is in flight -- keeps the buttons
// disabled even though the 5 Hz WS snapshot arriving mid-request still
// shows the order as PENDING (the backend is idempotent either way, but a
// disabled button during the round trip means there is never a moment to
// double-click into).
let orderOpBusy = false;

// hotkeys are the real interface during the demo; say so on the button
const hk = (k) => ` <span class="hk">${k}</span>`;

// ---------------------------------------------------------------------------
// static text from labels.js — nothing is hard-coded in index.html
// ---------------------------------------------------------------------------
function paintLabels() {
  $("t-title").textContent = L.title;
  $("t-sub").textContent = L.subtitle;

  $("h-plant").textContent = L.plant;
  $("l-art").textContent = L.article;
  $("l-qty").textContent = L.qty;
  $("l-ano").textContent = L.anomaly;
  $("btn-arrive").innerHTML = L.arrive + hk("A");
  $("btn-quick").textContent = L.quickBox;

  $("h-env").textContent = L.env;
  $("l-temp").textContent = L.temp;
  $("l-hum").textContent = L.hum;
  $("l-cure").textContent = L.cureNow;

  $("h-dem").textContent = L.demand;
  $("l-dref").textContent = L.article;
  $("l-dqty").textContent = L.qty;
  $("btn-demand").innerHTML = L.ask + hk("D");
  $("btn-confirm").innerHTML = L.confirm + hk("C");
  $("btn-cancel").textContent = L.cancel;

  $("h-kpi").textContent = L.warehouse;
  $("h-byref").textContent = L.byRef;
  $("h-prop").textContent = L.proposal;
  $("h-pending").textContent = L.pendingOrders;
  $("h-inv").textContent = L.inventory;
  $("h-log").textContent = L.events;

  $("btn-reset").textContent = L.reset;
  $("lnk-db").textContent = L.dbExplorer + " →";
  $("btn-scenario").innerHTML = L.scenario + hk("S");
  $("jump6").innerHTML = L.jump6 + hk("J");
  $("btn-more").title = L.more;
  $("btn-newref").textContent = "+ " + L.newRef;

  $("h-newref").textContent = L.newRef;
  $("l-refcode").textContent = L.refCode;
  $("l-reflabel").textContent = L.refLabel;
  $("l-refmass").textContent = L.unitMass;
  $("l-refcap").textContent = L.capacity;
  $("l-refcolor").textContent = L.color;
  $("btn-nref-add").textContent = L.addRef;
  $("btn-nref-cancel").textContent = L.cancelRef;

  $("byref").tHead.innerHTML =
    `<tr><th>${L.ref}</th><th class="num">${L.colReady}</th>` +
    `<th class="num">${L.colDrying}</th><th>${L.nextOut}</th></tr>`;
  $("invt").tHead.innerHTML =
    `<tr><th>${L.box}</th><th>${L.ref}</th><th>${L.state}</th>` +
    `<th>${L.slot}</th><th class="num">${L.age}</th><th>${L.cure}</th>` +
    `<th class="num">${L.qty}</th><th>${L.conf}</th></tr>`;

  // the 3D twin's slot colours, explained — one source of truth: twin.js COLOR
  $("legend").innerHTML = `<span>${L.legend}</span>` +
    ["DRYING", "READY", "RESERVED", "QUARANTINE"].map((s) =>
      `<span><i style="background:#${COLOR[s].toString(16).padStart(6, "0")}"></i>` +
      `${L.st[s]}</span>`).join("");
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
const simLabel = (t) => {
  const d = Math.floor(t / 86400), r = t - d * 86400;
  const h = Math.floor(r / 3600), m = Math.floor((r % 3600) / 60);
  return `J+${d} ${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
};

// algo/engine.py authors its rejection vocabulary in French, and
// algo/test_engine.py asserts on those exact strings — so the translation
// happens here, on the way to the screen. Unmatched text passes through.
const tRej = (s) => (L.rej && L.rej[s]) || s;
const tDet = (s) => {
  if (!s) return s;
  if (L.det && L.det[s]) return L.det[s];
  let m = /^pret dans ([\d.]+) h$/.exec(s);
  if (m) return L.detReadyIn(m[1]);
  m = /^ecart de comptage = (\d+) \(barriere (\d+) \/ pesee (\d+)\)$/.exec(s);
  if (m) return L.detDelta(m[1], m[2], m[3]);
  return s;
};

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------
function render(st) {
  ST = st;
  $("clock").textContent = st.clock_label;

  // which speed are we actually running at? (previously unanswerable)
  document.querySelectorAll("#seg-speed [data-speed]").forEach((b) =>
    b.classList.toggle("on", +b.dataset.speed === st.speed));

  $("p-mode").className = "pill mode" + (st.mode === "L0" ? "" : " fallback");
  $("p-mode").textContent = `${L.mode} ${st.mode}`;
  // the broker is folded into the device pill — two pills, not three
  $("p-dev").className = "pill " + (st.device.online ? "on" : "off");
  $("p-dev").textContent = `${L.device} ${st.device.online ? L.online : L.offline}` +
    (st.device.online ? ` · ${st.device.count_beam} · ${Math.round(st.device.gross_g)} g` : "");
  $("p-dev").title = `${L.broker} ${st.mqtt ? L.online : L.offline}`;

  // --- KPI: four tiles, each with its unit spelled out ---
  const k = st.kpi;
  const pct = k.slots_total ? (k.slots_used / k.slots_total) * 100 : 0;
  $("kpis").innerHTML = [
    `<div class="kpi acc"><div class="v">${k.slots_used}<small> / ${k.slots_total}</small></div>
       <div class="k">${L.kpiSlots}</div>
       <div class="bar acc" style="margin-top:6px"><i style="width:${pct}%"></i></div></div>`,
    `<div class="kpi ok"><div class="v">${k.cores_available}</div>
       <div class="k">${L.kpiCores}</div></div>`,
    `<div class="kpi warn"><div class="v">${k.boxes_drying}<small> ${L.boxes}</small></div>
       <div class="k">${L.kpiDrying}</div></div>`,
    `<div class="kpi bad"><div class="v">${k.boxes_quarantine}<small> ${L.boxes}</small></div>
       <div class="k">${L.kpiQuar}</div></div>`,
  ].join("");

  // --- CDC task 5: classify stock BY TYPE, with the FIFO head named ---
  $("byref").tBodies[0].innerHTML = (st.by_ref || []).map((r) => `
    <tr title="${r.label} — ${r.unit_mass_g} g${r.fifo_head
        ? ` · ${L.nextOut}: ${r.fifo_head} @ ${r.fifo_head_slot}` : ""}">
      <td><i class="sw" style="background:${r.color}"></i>${r.ref}</td>
      <td class="num ${r.ready ? "r-ready" : "r-none"}">${r.ready}</td>
      <td class="num ${r.drying ? "r-dry" : "r-none"}">${r.drying}</td>
      <td class="${r.fifo_head ? "" : "r-none"}">${r.fifo_head
        ? `${r.fifo_head} · ${r.fifo_head_age_h} h`
        : L.none}</td></tr>`).join("");

  // --- the FIFO proposal: picks AND, crucially, the rejections ---
  const o = st.last_order;
  $("order").innerHTML = !o ? `<div class="muted">${L.noOrder}</div>` : `
    <div class="order">
      <div class="hd"><span>${o.order_id} · ${o.ref}</span>
        <span>${o.qty_allocated}/${o.qty_requested}</span></div>
      ${o.shortfall ? `<div class="muted" style="color:#ff9a9a">${L.shortfall}: ${o.shortfall}</div>` : ""}
      <div class="muted" style="margin-top:6px">${L.picks}</div>
      ${o.picks.map((p) => `<div class="pick">
          <span>#${p.rank} ${p.box_id} · ${p.slot_id || "—"}</span>
          <span>${p.take} · ${simLabel(p.t_in_sim)}</span></div>`).join("") ||
        `<div class="muted">—</div>`}
      <div class="muted" style="margin-top:8px">${L.rejected}</div>
      ${o.rejected.map((r) => `<div class="rej">
          <span>${r.box_id}</span>
          <span><b>${tRej(r.reason)}</b> — ${tDet(r.detail)}</span>
        </div>`).join("") || `<div class="muted">—</div>`}
    </div>`;

  // a fresh order is the thing you just pressed D for: put it on screen even
  // when the right column is taller than a 768 px projector
  if (o && o.order_id !== lastOrderId) {
    // only for an order raised during this session -- scrolling the KPIs
    // away the instant the page loads is not what anyone asked for
    if (!firstRender) $("h-prop").scrollIntoView({ block: "start", behavior: "smooth" });
    lastOrderId = o.order_id;
  } else if (!o) lastOrderId = null;

  // no order to act on -> no live Confirm / Cancel to mis-click. While a
  // confirm/cancel round trip is in flight (orderOpBusy), stay disabled
  // regardless of what this snapshot says -- it can be mid-flight-stale.
  const actionable = !!o && o.status === "PENDING";
  $("btn-confirm").disabled = !actionable || orderOpBusy;
  $("btn-cancel").disabled = !actionable || orderOpBusy;

  // --- inventory: the six columns the CDC asks for, always on screen ---
  const filt = $("filt").value || "*";
  $("invt").tBodies[0].innerHTML = st.boxes.filter(
      (b) => filt === "*" || b.ref === filt).map((b) => {
    const picked = (o?.picks || []).some((p) => p.box_id === b.box_id);
    const done = b.cure_pct >= 100;
    return `<tr class="${picked ? "sel" : ""}" title="${tDet(b.reason) || ""}">
      <td>${b.box_id}</td><td>${b.ref}</td>
      <td><span class="tag s-${b.state}">${L.st[b.state] || b.state}</span></td>
      <td>${b.slot_id || "—"}</td>
      <td class="num">${b.age_h} h</td>
      <td><div class="cure">
          <div class="bar ${done ? "done" : ""}"><i style="width:${b.cure_pct}%"></i></div>
          <span class="h">${done ? "✓" : b.h_remaining + " h"}</span></div></td>
      <td class="num">${b.qty_available}/${b.qty_initial}</td>
      <td>${b.confidence}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">—</td></tr>`;

  // --- banner ---
  if (st.banner && st.banner.t_sim !== lastBannerT) {
    lastBannerT = st.banner.t_sim;
    const el = $("banner");
    el.className = st.banner.kind;
    el.textContent = st.banner.text;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = ""; }, 6000);
  }

  // the server owns the climate; the sliders only mirror it (unless dragging).
  // Climate is monitoring-only (contract 1.2): it no longer feeds cure math,
  // so it never changes v-cure -- that is a fixed 24 h label, painted once.
  if (document.activeElement !== $("t_c") && document.activeElement !== $("rh")) {
    $("t_c").value = st.env.t_c;
    $("rh").value = st.env.rh;
    $("v-t").textContent = (+st.env.t_c).toFixed(1);
    $("v-rh").textContent = st.env.rh;
  }

  renderPendingOrders(st);
  updateTwin(st);
  firstRender = false;
}

// ---------------------------------------------------------------------------
// pending reservations -- every PENDING order, not just the last one, with
// a live lock countdown so a presenter can see a reservation about to
// expire before it happens (contract 1.2 §6.5 / demo beat 6).
// ---------------------------------------------------------------------------
function renderPendingOrders(st) {
  const el = $("pending-orders");
  if (!el) return;
  const rows = st.orders_pending || [];
  el.innerHTML = !rows.length ? "" : rows.map((o) => {
    const remaining = o.lock_remaining_s;
    const label = remaining == null ? "" :
      remaining > 0 ? L.lockExpiresIn(Math.max(0, Math.round(remaining))) : L.lockExpired;
    return `<div class="pick">
        <span>${o.order_id} · ${o.ref} · ${o.qty_allocated}/${o.qty_requested}</span>
        <span>${label}</span></div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// activity log — one readable line per event instead of a wall of JSON.
// Every kind logged by backend/warehouse.py or backend/main.py::event() has
// a case below; an unknown kind still renders (as its payload), so a new
// one cannot blank this.
// ---------------------------------------------------------------------------
function eventBits(kind, p) {
  switch (kind) {
    case "box_in": return [p.box_id, p.ref, `${p.qty} ${L.cores}`, p.slot,
                           p.confidence, p.delta ? `Δ${p.delta}` : null];
    case "quarantine": return [p.box_id, p.ref || p.declared_ref, tDet(p.reason)];
    case "cured": return [p.box_id, `${p.after_h} h`];
    case "demand": return [p.order_id, p.ref, `${p.allocated}/${p.qty}`,
                           (p.picks || []).join(" + ") || null,
                           p.rejected ? `${p.rejected} ${L.refusedShort}` : null];
    case "pick_done": return [p.order_id, `${p.qty} ${L.cores}`,
                              (p.boxes || []).map((b) => b.box_id).join(" + ") || null];
    case "order_cancel": return [p.order_id, (p.released || []).join(" + ") || null];
    case "order_expired": return [p.order_id, (p.released || []).join(" + ") || null];
    case "clock_jump": return [`+${p.hours} h`];
    case "clock_speed": return [`×${p.speed}`];
    case "env": return [`${(+p.t_c).toFixed(1)} °C`, `${p.rh} %`];
    case "article_new": return [p.ref, p.label, `${p.unit_mass_g} g`];
    case "box_done_ignored": return [p.ref, tDet(p.reason)];
    case "box_done_invalid": return [p.error];
    case "arrival_fallback": return [p.box_id, p.ref, tDet(p.reason)];
    case "system_reset": return [p.keep_articles ? "seed=false" : null];
    case "scenario_loaded": return [p.name];
    default: return [JSON.stringify(p)];
  }
}

async function renderLog() {
  const evs = await api("/events?limit=60");
  $("logbody").innerHTML = evs.map((e) => {
    const p = e.payload || {};
    const bits = eventBits(e.kind, p).filter((x) => x !== null && x !== undefined);
    return `<div class="ev"><span class="t">${simLabel(e.t_sim)}</span>` +
      `<span class="k">${(L.ev && L.ev[e.kind]) || e.kind}</span>` +
      `<span class="p">${bits.join(" · ")}</span></div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------
// article dropdowns are rebuilt from the DB every time the list can have
// changed (on boot, and after "+ New reference" succeeds) — nothing here is
// a fixed set of refs
async function refreshArticles() {
  ARTICLES = await api("/articles");
  const opts = ARTICLES.map((a) =>
    `<option value="${a.ref}">${a.ref} — ${a.label} (${a.unit_mass_g} g)</option>`).join("");
  const prevArt = $("art").value, prevDref = $("dref").value, prevFilt = $("filt").value;
  $("art").innerHTML = opts;
  $("dref").innerHTML = opts;
  $("filt").innerHTML = `<option value="*">${L.filterAll}</option>` +
    ARTICLES.map((a) => `<option value="${a.ref}">${a.ref}</option>`).join("");
  // keep whatever the operator had selected if it still exists (a fresh
  // "New reference" call should not silently reset the plant-model picker)
  if (ARTICLES.some((a) => a.ref === prevArt)) $("art").value = prevArt;
  if (ARTICLES.some((a) => a.ref === prevDref)) $("dref").value = prevDref;
  if (prevFilt === "*" || ARTICLES.some((a) => a.ref === prevFilt)) $("filt").value = prevFilt;
}

async function boot() {
  paintLabels();
  initTwin($("cv"), $("hud"));

  await refreshArticles();
  $("filt").onchange = () => ST && render(ST);

  const anos = await api("/anomalies");
  $("ano").innerHTML = Object.entries(anos)
    .map(([k, v]) => `<option value="${k}">${v}</option>`).join("");

  document.querySelectorAll("[data-speed]").forEach((b) =>
    b.onclick = () => api("/clock", { speed: +b.dataset.speed }));
  $("jump6").onclick = () => api("/clock", { jump_h: 6 });
  // Scenario reseeds articles too (backend/warehouse.py::load_demo_scenario
  // always does a full seed) -- refresh the dropdowns so a reference added
  // via "+ New reference" before pressing S does not linger as a stale
  // <option> pointing at a row that no longer exists.
  $("btn-scenario").onclick = () =>
    api("/scenario", { name: "demo" }).then(() => { refreshArticles(); renderLog(); });

  // ⋯ overflow menu — Reset and DB Explorer live in here so the top bar can
  // stay down to one line of things you actually press during the demo
  const closeMenu = () => {
    $("more-pop").hidden = true;
    $("btn-more").setAttribute("aria-expanded", "false");
  };
  $("btn-more").onclick = (e) => {
    e.stopPropagation();
    const open = $("more-pop").hidden;
    $("more-pop").hidden = !open;
    $("btn-more").setAttribute("aria-expanded", String(open));
  };
  addEventListener("click", closeMenu);
  $("btn-reset").onclick = () => {
    closeMenu();
    api("/reset", {}).then(() => { refreshArticles(); renderLog(); });
  };

  // "+ New reference" modal — POST /api/articles (docs/contracts.md §2)
  const openRefModal = () => {
    closeMenu();
    $("nref-ref").value = ""; $("nref-label").value = "";
    $("nref-mass").value = ""; $("nref-cap").value = "";
    $("nref-color").value = "#4f8cff";
    $("nref-err").hidden = true;
    $("refModal").hidden = false;
    $("nref-ref").focus();
  };
  const closeRefModal = () => { $("refModal").hidden = true; };
  $("btn-newref").onclick = openRefModal;
  $("btn-nref-cancel").onclick = closeRefModal;
  $("refModal").onclick = (e) => { if (e.target === $("refModal")) closeRefModal(); };
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("refModal").hidden) closeRefModal();
  });
  $("btn-nref-add").onclick = async () => {
    const body = {
      ref: $("nref-ref").value.trim(), label: $("nref-label").value.trim(),
      unit_mass_g: +$("nref-mass").value, color: $("nref-color").value,
    };
    if ($("nref-cap").value) body.box_capacity = +$("nref-cap").value;
    if (!body.ref || !body.label || !(body.unit_mass_g > 0)) {
      $("nref-err").textContent = L.refCode + " / " + L.refLabel + " / " + L.unitMass;
      $("nref-err").hidden = false;
      return;
    }
    const b = $("btn-nref-add");
    b.disabled = true;
    const res = await api("/articles", body);
    b.disabled = false;
    if (res.error) {
      $("nref-err").textContent = res.error;
      $("nref-err").hidden = false;
      return;
    }
    await refreshArticles();
    closeRefModal();
    renderLog();
  };

  $("btn-arrive").onclick = async () => {
    const b = $("btn-arrive");
    b.disabled = true; b.textContent = "…";
    await api("/sim/arrival", { ref: $("art").value, qty: +$("qty").value,
                                anomaly: $("ano").value });
    b.disabled = false; b.innerHTML = L.arrive + hk("A");
    renderLog();
  };
  $("btn-quick").onclick = () =>
    api("/sim/box", { ref: $("art").value, qty: +$("qty").value }).then(renderLog);

  // Live label feedback on every drag tick; the actual POST (and the event
  // it logs) only fires on release, not per pixel of drag (plan §18 -- the
  // old version flooded the event log with one "env" row per input tick).
  const previewEnv = () => {
    $("v-t").textContent = (+$("t_c").value).toFixed(1);
    $("v-rh").textContent = $("rh").value;
  };
  const pushEnv = () => {
    previewEnv();
    api("/sim/env", { t_c: +$("t_c").value, rh: +$("rh").value });
  };
  $("t_c").oninput = previewEnv;
  $("rh").oninput = previewEnv;
  $("t_c").onchange = pushEnv;
  $("rh").onchange = pushEnv;

  // In-flight guards: the backend is idempotent either way (double-confirm
  // is a documented no-op, not a double deduction), but disabling the
  // button for the round trip means a held key or a double click never
  // needs that safety net, and there is never a moment where the button
  // looks live while the order has already changed under it.
  const guardedOrderOp = (btn, fn) => {
    btn.onclick = async () => {
      if (btn.disabled || orderOpBusy) return;
      orderOpBusy = true;
      btn.disabled = true;
      try { await fn(); } finally { orderOpBusy = false; if (ST) render(ST); }
    };
  };
  $("btn-demand").onclick = async () => {
    const b = $("btn-demand");
    if (b.disabled) return;
    b.disabled = true;
    try {
      await api("/demand", { ref: $("dref").value, qty: +$("dqty").value });
      renderLog();
    } finally { b.disabled = false; }
  };
  guardedOrderOp($("btn-confirm"), () => ST?.last_order &&
    api("/demand/confirm", { order_id: ST.last_order.order_id }).then(renderLog));
  guardedOrderOp($("btn-cancel"), () => ST?.last_order &&
    api("/demand/cancel", { order_id: ST.last_order.order_id }).then(renderLog));

  document.querySelectorAll("[data-cam]").forEach((b) =>
    b.onclick = () => setCamera(b.dataset.cam));
  $("btn-follow").onclick = (e) =>
    e.target.style.borderColor = toggleFollow() ? "#4f8cff" : "";

  connect();
  renderLog();
  setInterval(renderLog, 4000);
}

let pollTimer = null;

function startPolling() {
  // Insurance: if the WebSocket cannot be established (a uvicorn built without
  // a websocket library is the classic one), the dashboard still updates. The
  // screen is identical; only the refresh rate drops.
  if (pollTimer) return;
  console.warn("[scw] WebSocket unavailable - falling back to polling");
  pollTimer = setInterval(() => api("/state").then(render).catch(() => {}), 500);
}

function connect() {
  let ws;
  try {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  } catch (e) { return startPolling(); }
  const guard = setTimeout(startPolling, 2500);   // never opened -> poll
  ws.onopen = () => {
    clearTimeout(guard);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onerror = () => startPolling();
  ws.onclose = () => setTimeout(connect, 1000);   // survives a backend restart
}

// --- demo hotkeys: rehearse these, they are faster than hunting for buttons --
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  const map = {
    a: () => $("btn-arrive").click(),
    d: () => $("btn-demand").click(),
    c: () => !$("btn-confirm").disabled && $("btn-confirm").click(),
    j: () => api("/clock", { jump_h: 6 }),
    r: () => $("btn-reset").click(),
    s: () => $("btn-scenario").click(),
    1: () => setCamera("iso"), 2: () => setCamera("aisle"),
    3: () => setCamera("front"), 4: () => setCamera("top"),
  };
  if (map[e.key]) { e.preventDefault(); map[e.key](); }
});

boot();
