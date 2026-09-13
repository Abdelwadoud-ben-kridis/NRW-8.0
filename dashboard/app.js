// app.js — the HMI half of the dashboard (criterion 8, 15 pts).
// No framework, no build step. One WebSocket in, one DOM out.
// Every visible number comes from GET /api/state or the /ws snapshot; the
// only client-side "logic" is presentation (sorting, filtering, colour
// thresholds, FR/EN strings) -- see docs/contracts.md for what the backend
// actually guarantees.

import { L, setLang } from "./labels.js";
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
let orderOpBusy = false;
let activeView = "rack";        // "rack" | "twin"
let selectedSlot = null;        // slot_id currently pinned in the inspector
let invSort = { key: "t_in_sim", dir: 1 };
let invSearch = "";
let refAvail = {};        // ref -> total READY cores, refreshed every snapshot (st.by_ref)

const hk = (k) => ` <span class="hk">${k}</span>`;

// ---------------------------------------------------------------------------
// static text from labels.js — nothing is hard-coded in index.html
// ---------------------------------------------------------------------------
function paintLabels() {
  $("t-title").textContent = L.title;
  $("t-sub").textContent = L.subtitle;
  $("btn-lang").textContent = L.lang;
  $("btn-lang").title = L.lang === "EN" ? "Français" : "English";
  $("btn-sim").title = L.simControls;
  $("btn-shortcuts").title = L.shortcuts;
  $("contract-chip").textContent = `${L.contract} 1.4`;
  $("db-health").title = L.database;

  $("h-plant").textContent = L.plant;
  $("l-art").textContent = L.article;
  $("l-qty").textContent = L.qty;
  $("l-ano").textContent = L.anomaly;
  $("btn-arrive").innerHTML = L.arrive + hk("A");
  $("btn-quick").textContent = L.quickBox;

  $("h-env").textContent = L.env;
  $("h-env2").textContent = L.env;
  $("l-temp").textContent = L.temp;
  $("l-hum").textContent = L.hum;
  $("l-cure").textContent = L.cureNow;
  $("env-t-k").textContent = L.temp;
  $("env-rh-k").textContent = L.hum;
  $("env-notice").textContent =
    (L.lang === "EN"
      ? "Temperature and humidity are evidence only — they never change the fixed 24 h cure requirement."
      : "Température et humidité sont uniquement des relevés — elles ne modifient jamais le séchage fixe de 24 h.");

  $("h-dem").textContent = L.demand;
  $("l-dref").textContent = L.article;
  $("l-dqty").textContent = L.qty;
  $("btn-demand").innerHTML = L.ask + hk("D");
  $("btn-confirm").innerHTML = L.confirm + hk("C");
  $("btn-cancel").textContent = L.cancel;

  $("h-byref").textContent = L.byRef;
  $("h-prop").textContent = L.proposal;
  $("h-pending").textContent = L.pendingOrders;
  $("h-inv").textContent = L.inventory;
  $("h-log").textContent = L.events;
  $("h-liveop").textContent = L.liveOperation;
  $("h-esp32").textContent = L.liveEsp32;
  $("h-curing-txt").textContent = L.curingModule;
  $("h-crane").textContent = L.stackerCrane;
  $("h-quar-txt").textContent = L.quarantineModule;
  $("h-shortcuts").textContent = L.shortcuts;

  $("invsearch").placeholder = L.searchPlaceholder;
  $("tab-rack").textContent = L.rackView;
  $("tab-twin").textContent = L.twinView;

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

  paintInvHead();

  $("rack-legend").innerHTML = `<span>${L.legend}</span>` +
    ["DRYING", "READY", "RESERVED"].map((s) =>
      `<span><i style="background:#${COLOR[s].toString(16).padStart(6, "0")}"></i>${L.st[s]}</span>`)
      .join("") +
    `<span><i style="background:#141414;border:1px solid rgba(255,255,255,.15)"></i>${L.lang === "EN" ? "Free" : "Libre"}</span>`;
  $("legend").innerHTML = `<span>${L.legend}</span>` +
    ["DRYING", "READY", "RESERVED", "QUARANTINE"].map((s) =>
      `<span><i style="background:#${COLOR[s].toString(16).padStart(6, "0")}"></i>` +
      `${L.st[s]}</span>`).join("");

  buildShortcuts();
}

const INV_COLS = [
  { key: "box_id", label: () => L.box },
  { key: "ref", label: () => L.ref },
  { key: "label", label: () => L.label2 },
  { key: "qty_initial", label: () => L.qty, num: true },
  { key: "qty_available", label: () => L.available, num: true },
  { key: "state", label: () => L.state },
  { key: "slot_id", label: () => L.slot },
  { key: "t_in_sim", label: () => L.arrival, num: true },
  { key: "cure_pct", label: () => L.cure, num: true },
  { key: "confidence", label: () => L.conf },
  { key: "locked_by", label: () => L.lock },
];

function paintInvHead() {
  $("invt").tHead.innerHTML = "<tr>" + INV_COLS.map((c) => {
    const cls = [];
    if (c.num) cls.push("num");
    if (invSort.key === c.key) cls.push(invSort.dir > 0 ? "sort-asc" : "sort-desc");
    return `<th data-key="${c.key}" class="${cls.join(" ")}">${c.label()}</th>`;
  }).join("") + "</tr>";
  $("invt").tHead.querySelectorAll("th").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.key;
      if (invSort.key === k) invSort.dir *= -1;
      else { invSort.key = k; invSort.dir = 1; }
      paintInvHead();
      if (ST) render(ST);
    };
  });
}

function buildShortcuts() {
  const rows = [
    ["A", L.arrive], ["D", L.ask], ["C", L.confirm],
    ["J", L.jump6], ["S", L.scenario], ["R", L.reset],
    ["1 / 2 / 3 / 4", L.lang === "EN" ? "3D twin cameras" : "Caméras du jumeau 3D"],
    ["?", L.shortcuts],
  ];
  $("shortcuts-list").innerHTML = rows.map(([k, label]) =>
    `<div class="row2"><span>${label}</span><span class="kbd">${k}</span></div>`).join("");
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
  m = /^quantite (\d+) superieure a la capacite de la caisse \((\d+)\)$/.exec(s);
  if (m) return L.detOverCapacity(m[1], m[2]);
  return s;
};

// ---------------------------------------------------------------------------
// WAREHOUSE RACK — 2 faces x 9 columns x 17 levels = 306 slots (fixed
// physical geometry, backend/config.py FACES/COLS/LEVELS). Built once;
// every render() only mutates class/title on the existing 306 nodes.
// ---------------------------------------------------------------------------
const RACK_FACES = 2, RACK_COLS = 9, RACK_LEVELS = 17;
const rackCells = {};          // slot_id -> <div>
let prevRackState = {};        // slot_id -> state, for arrival/ready flashes

function buildRack() {
  const root = $("rack-faces");
  root.innerHTML = "";
  for (let f = 0; f < RACK_FACES; f++) {
    if (f > 0) {
      // aisle divider between faces — this rack is two racks either side
      // of the crane aisle (backend/config.py FACES), not one continuous wall
      const aisle = document.createElement("div");
      aisle.className = "rackaisle";
      aisle.innerHTML = `<div class="line"></div><div class="txt">AISLE</div><div class="line"></div>`;
      root.appendChild(aisle);
    }
    const face = document.createElement("div");
    face.className = "rackface";
    const label = document.createElement("div");
    label.className = "facelabel";
    label.textContent = `FACE ${f}`;
    face.appendChild(label);

    const grid = document.createElement("div");
    grid.className = "rackgrid";
    grid.style.setProperty("--cols", RACK_COLS);
    grid.appendChild(Object.assign(document.createElement("div"), { className: "rackcorner" }));
    for (let c = 0; c < RACK_COLS; c++) {
      const h = document.createElement("div");
      h.className = "rackcolhead";
      h.textContent = c;
      grid.appendChild(h);
    }
    for (let lvl = RACK_LEVELS - 1; lvl >= 0; lvl--) {
      const lh = document.createElement("div");
      lh.className = "racklevelhead" + (lvl % 4 === 0 ? " major" : "");
      lh.textContent = "L" + lvl;
      grid.appendChild(lh);
      for (let c = 0; c < RACK_COLS; c++) {
        const slotId = `F${f}-C${c}-L${lvl}`;
        const cell = document.createElement("div");
        cell.className = "rackslot";
        cell.dataset.slot = slotId;
        cell.title = slotId;
        cell.onclick = () => openSlotInspector(slotId);
        rackCells[slotId] = cell;
        grid.appendChild(cell);
      }
    }
    face.appendChild(grid);
    root.appendChild(face);
  }
}

function updateRack(st) {
  const bySlot = {};
  for (const b of st.boxes) if (b.slot_id) bySlot[b.slot_id] = b;
  for (const [slotId, cell] of Object.entries(rackCells)) {
    const b = bySlot[slotId];
    const state = b ? b.state : null;
    const prev = prevRackState[slotId];
    cell.className = "rackslot" + (state ? ` st-${state}` : "") +
      (selectedSlot === slotId ? " sel" : "");
    cell.title = b
      ? `${slotId} · ${b.box_id} · ${b.ref || L.unknownRef} · ${L.st[b.state] || b.state}`
      : `${slotId} — ${L.slotFree}`;
    if (b && !prev) { cell.classList.add("arrived"); setTimeout(() => cell.classList.remove("arrived"), 900); }
    else if (prev === "DRYING" && state === "READY") {
      cell.classList.add("just-ready"); setTimeout(() => cell.classList.remove("just-ready"), 1400);
    }
    prevRackState[slotId] = state;
  }
  if (selectedSlot) renderSlotInspector(bySlot[selectedSlot], selectedSlot);
}

function openSlotInspector(slotId) {
  selectedSlot = slotId;
  $("slot-inspector").classList.add("open");
  const b = ST && ST.boxes.find((x) => x.slot_id === slotId);
  renderSlotInspector(b, slotId);
  if (ST) updateRack(ST);
}
function closeSlotInspector() {
  selectedSlot = null;
  $("slot-inspector").classList.remove("open");
  if (ST) updateRack(ST);
}
function renderSlotInspector(b, slotId) {
  if (!b) {
    $("insp-body").innerHTML = `<div class="insp-title">${slotId}</div>
      <div class="insp-empty">${L.slotFree}</div>`;
    return;
  }
  const rows = [
    [L.reference, b.ref || L.unknownRef],
    [L.label2, b.label || "—"],
    [L.quantity, b.qty_initial],
    [L.available, b.qty_available],
    [L.state, L.st[b.state] || b.state],
    [L.slot, b.slot_id || "—"],
    [L.cure, `${b.cure_pct}%`],
    [L.conf, b.confidence || "—"],
    [L.arrival, simLabel(b.t_in_sim)],
    [L.reason2, tDet(b.reason) || L.noReason],
    [L.lock, b.locked_by || L.noLock],
  ];
  $("insp-body").innerHTML = `<div class="insp-title">${b.box_id}</div>
    <div class="insp-slot">${slotId}</div>` +
    rows.map(([k, v]) => `<div class="insp-field"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");
}

// ---------------------------------------------------------------------------
// LIVE OPERATION — a presentation-only "current stage" derived from real,
// already-exposed signals (device.state, crane.cmd, last_order, banner).
// This never invents backend truth; it just narrates it (spec: "present
// virtual stages intelligently, never as persisted DB states").
// ---------------------------------------------------------------------------
// crane.seq only increments on a real store/pick command (backend/main.py);
// tracking it here (not on the snapshot) lets the stage stay "STORING" /
// "ALLOCATING" for a few seconds after the seq bump instead of forever.
let _lastCraneSeq = null, _lastCraneChangeMs = 0;

function deriveStage(st) {
  if (st.crane.seq !== _lastCraneSeq) {
    _lastCraneSeq = st.crane.seq;
    _lastCraneChangeMs = Date.now();
  }
  const craneRecent = (Date.now() - _lastCraneChangeMs) < 4000;

  if (st.device.state === "FAULT") return ["fault", L.stageFault, ""];
  if (st.banner && st.banner.kind === "quarantine" && (st.t_sim - st.banner.t_sim) < 8)
    return ["fault", L.stageQuarantine, ""];
  if (st.device.online && st.device.state === "COUNTING")
    return ["active", L.stageCounting, L.stageSub];
  if (st.device.online && st.device.state === "STABILIZING")
    return ["active", L.stageStabilizing, L.stageSub];
  if (st.crane.cmd === "store" && craneRecent)
    return ["active", L.stageStoring, L.stageSub];
  if (st.crane.cmd === "pick" && craneRecent && st.last_order && st.last_order.status === "PENDING")
    return ["active", L.stageAllocating, L.stageSub];
  if (st.last_order && st.last_order.status === "PENDING")
    return ["curing", L.stageReserved, L.stageSub];
  if (st.kpi.boxes_drying > 0 && st.kpi.boxes_ready === 0 && !st.last_order)
    return ["curing", L.stageCuring, L.stageSub];
  return ["idle", L.stageIdle, L.stageIdleSub];
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------
function render(st) {
  ST = st;
  $("clock").innerHTML = `${st.clock_label} <small id="clock-speed">×${st.speed}</small>`;

  document.querySelectorAll("#seg-speed [data-speed]").forEach((b) =>
    b.classList.toggle("on", +b.dataset.speed === st.speed));

  $("p-mode").className = "pill mode" + (st.mode === "L0" ? "" : " fallback");
  $("p-mode").textContent = `${L.mode} ${st.mode}`;
  $("p-dev").className = "pill " + (st.device.online ? "on" : "off");
  $("p-dev").textContent = `${L.device} ${st.device.online ? L.online : L.offline}`;

  // --- KPI ribbon: six tiles, each with its unit spelled out ---
  const k = st.kpi;
  const pct = k.slots_total ? (k.slots_used / k.slots_total) * 100 : 0;
  $("kpi-ribbon").innerHTML = [
    `<div class="kpi-tile acc"><div class="v">${k.slots_total}</div><div class="k">${L.lang === "EN" ? "Total slots" : "Emplacements totaux"}</div></div>`,
    `<div class="kpi-tile acc"><div class="v">${k.slots_used}<small> / ${k.slots_total}</small></div>
       <div class="k">${L.kpiSlots}</div>
       <div class="bar" style="margin-top:6px"><i style="width:${pct}%"></i></div></div>`,
    `<div class="kpi-tile ok"><div class="v">${k.boxes_ready}</div><div class="k">${L.kpiReady}</div></div>`,
    `<div class="kpi-tile warn"><div class="v">${k.boxes_drying}</div><div class="k">${L.kpiDrying}</div></div>`,
    `<div class="kpi-tile bad${k.boxes_quarantine ? "" : " zero"}"><div class="v">${k.boxes_quarantine}</div><div class="k">${L.kpiQuar}</div></div>`,
    `<div class="kpi-tile ok"><div class="v">${k.cores_available}</div><div class="k">${L.kpiCores}</div></div>`,
  ].join("");

  // --- live operation stage ---
  const [cls, label, sub] = deriveStage(st);
  $("stagecard").querySelector(".stagebox").className = "stagebox stage-" + cls;
  $("stage-label").textContent = label;
  $("stage-sub").textContent = sub;

  // --- ESP32 instrument panel ---
  const dev = st.device;
  $("esp32-online").className = "pill " + (dev.online ? "on" : "off");
  $("esp32-online").textContent = dev.online ? L.online : L.offline;
  $("esp32-state").className = "esp32-state st-" + dev.state;
  $("esp32-state").textContent = L.esp32St[dev.state] || dev.state;
  $("esp32-beam").textContent = dev.online ? dev.count_beam : "—";
  $("esp32-beam-k").textContent = L.beamCount;
  $("esp32-mass").textContent = dev.online ? `${(dev.gross_g / 1000).toFixed(2)} kg` : "—";
  $("esp32-mass-k").textContent = L.grossMass;
  $("esp32-stable").textContent = dev.online ? (dev.stable ? `● ${L.stable}` : `○ ${L.unstable}`) : "";
  $("esp32-lastseen").textContent = dev.online && dev.last_seen_sim > -1e8
    ? `${L.lastSeen}: ${simLabel(dev.last_seen_sim)}` : L.noSignalYet;

  // --- environment (monitoring only — never feeds cure math) ---
  $("env-t").textContent = `${(+st.env.t_c).toFixed(1)}°`;
  $("env-rh").textContent = `${st.env.rh}%`;
  if (document.activeElement !== $("t_c") && document.activeElement !== $("rh")) {
    $("t_c").value = st.env.t_c;
    $("rh").value = st.env.rh;
    $("v-t").textContent = (+st.env.t_c).toFixed(1);
    $("v-rh").textContent = st.env.rh;
  }

  // --- curing preview (left column) ---
  const drying = st.boxes.filter((b) => b.state === "DRYING")
    .slice().sort((a, b2) => b2.cure_pct - a.cure_pct);
  $("curing-cnt").textContent = drying.length || "";
  $("curing-list").innerHTML = !drying.length
    ? `<div class="muted">${L.curingNone}</div>`
    : drying.slice(0, 6).map((b) => `
      <div class="cure-row">
        <span class="id">${b.box_id}</span>
        <span class="bar${b.cure_pct >= 100 ? " done" : ""}"><i style="width:${b.cure_pct}%"></i></span>
        <span class="pct">${b.cure_pct}%</span>
      </div>`).join("") +
      (drying.length > 6 ? `<div class="muted" style="margin-top:4px">${L.curingMore(drying.length - 6)}</div>` : "");

  // --- crane ---
  const cr = st.crane;
  $("crane-body").innerHTML = cr.cmd === "idle" || !cr.box_id
    ? `<div class="crane-empty">${L.craneNoCmd}</div>`
    : `<div class="crane-line">
         <span class="crane-cmd ${cr.cmd}">${cr.cmd === "store" ? L.craneStore : L.cranePick}</span>
         <span>${cr.box_id}</span>
         <span class="crane-arrow">→</span>
         <span>${cr.slot_id || "—"}</span>
       </div>`;

  // --- quarantine module ---
  const quar = st.boxes.filter((b) => b.state === "QUARANTINE");
  $("quarcard").hidden = quar.length === 0;
  $("quar-cnt").textContent = quar.length || "";
  if (quar.length) {
    $("quar-list").innerHTML = quar.map((b) => {
      const m = /^ecart de comptage = (\d+) \(barriere (\d+) \/ pesee (\d+)\)$/.exec(b.reason || "");
      return `<div class="quar-row">
        <div class="hd"><span>${b.box_id}</span><span>${b.ref || L.unknownRef}</span></div>
        <div class="why">${tDet(b.reason) || "—"}</div>
        ${m ? `<div class="evid"><span>${L.barrier} <b>${m[2]}</b></span>
                <span>${L.weight} <b>${m[3]}</b></span>
                <span>${L.delta} <b>${m[1]}</b></span></div>` : ""}
      </div>`;
    }).join("");
  }

  // --- rack + slot inspector ---
  updateRack(st);

  // --- CDC task 5: classify stock BY TYPE, with the FIFO head named ---
  refAvail = {};
  (st.by_ref || []).forEach((r) => { refAvail[r.ref] = r.ready; });
  updateDemandHint();
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
      <div class="sub muted">
        <span>${o.status === "CANCELLED" ? L.reservationReleased : (o.status || "")}</span>
        ${o.shortfall ? `<span style="color:#ff9a9a">${L.shortfall}: ${o.shortfall}</span>` : ""}
      </div>
      <div class="muted" style="margin-top:6px">${L.picks}</div>
      ${(o.picks || []).map((p) => `<div class="pick">
          <span><span class="rank">#${p.rank}</span>${p.box_id} · ${p.slot_id || "—"}</span>
          <span>${p.take} · ${simLabel(p.t_in_sim)}</span></div>`).join("") ||
        `<div class="muted">—</div>`}
      <div class="rejtitle">${L.rejected}</div>
      ${(o.rejected || []).map((r) => `<div class="rej">
          <span class="bid">${r.box_id}</span>
          <span><b>${tRej(r.reason)}</b> — ${tDet(r.detail)}</span>
        </div>`).join("") || `<div class="muted">—</div>`}
    </div>`;

  if (o && o.order_id !== lastOrderId) {
    if (!firstRender) $("h-prop").scrollIntoView({ block: "start", behavior: "smooth" });
    lastOrderId = o.order_id;
  } else if (!o) lastOrderId = null;

  const actionable = !!o && o.status === "PENDING";
  $("btn-confirm").disabled = !actionable || orderOpBusy;
  $("btn-cancel").disabled = !actionable || orderOpBusy;

  // --- inventory: BOX / REF / LABEL / QTY / AVAILABLE / STATE / SLOT /
  //     ARRIVAL / CURE / CONFIDENCE / LOCK -- searchable, sortable, filterable
  const filt = $("filt").value || "*";
  const q = invSearch.trim().toLowerCase();
  let rows = st.boxes.filter((b) => filt === "*" || b.state === filt || b.ref === filt);
  if (q) rows = rows.filter((b) =>
    (b.box_id || "").toLowerCase().includes(q) ||
    (b.ref || "").toLowerCase().includes(q) ||
    (b.label || "").toLowerCase().includes(q));
  rows = rows.slice().sort((a, b) => {
    const va = a[invSort.key], vb = b[invSort.key];
    const cmp = typeof va === "string" ? va.localeCompare(vb || "") : (va ?? -1) - (vb ?? -1);
    return cmp * invSort.dir;
  });
  $("invt").tBodies[0].innerHTML = rows.map((b) => {
    const picked = (o?.picks || []).some((p) => p.box_id === b.box_id);
    const done = b.cure_pct >= 100;
    return `<tr class="${picked ? "sel" : ""}" title="${tDet(b.reason) || ""}">
      <td>${b.box_id}</td><td>${b.ref || "—"}</td><td>${b.label || "—"}</td>
      <td class="num">${b.qty_initial}</td><td class="num">${b.qty_available}</td>
      <td><span class="tag s-${b.state}">${L.st[b.state] || b.state}</span></td>
      <td>${b.slot_id || "—"}</td>
      <td>${simLabel(b.t_in_sim)}</td>
      <td><div class="cure">
          <div class="bar ${done ? "done" : ""}"><i style="width:${b.cure_pct}%"></i></div>
          <span class="h">${done ? "✓" : b.cure_pct + "%"}</span></div></td>
      <td>${b.confidence || "—"}</td>
      <td>${b.locked_by || "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="${INV_COLS.length}" class="muted">—</td></tr>`;

  // --- banner ---
  if (st.banner && st.banner.t_sim !== lastBannerT) {
    lastBannerT = st.banner.t_sim;
    const el = $("banner");
    el.className = st.banner.kind;
    el.textContent = st.banner.text;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = ""; }, 6000);
  }

  renderPendingOrders(st);
  if (activeView === "twin") updateTwin(st);
  firstRender = false;
}

// ---------------------------------------------------------------------------
// reservation panel — every PENDING order, with a live lock countdown whose
// colour state (normal/warning/critical/expired) narrates how close the
// backend's real expiry sweep (backend/warehouse.py::sweep_expired) is, PLUS
// a Confirm/Cancel action per row: backend/warehouse.py::confirm/cancel
// already accept any order_id, but the top "proposal" panel above only ever
// targets last_order -- this is how an OLDER pending order (one that isn't
// the most recent demand) gets taken out or released.
// ---------------------------------------------------------------------------
const pendingOpBusy = new Set();   // order_ids with a confirm/cancel in flight

function renderPendingOrders(st) {
  const el = $("pending-orders");
  const rows = st.orders_pending || [];
  el.innerHTML = !rows.length ? `<div class="muted">${L.noPending}</div>` : rows.map((o) => {
    const remaining = o.lock_remaining_s;
    let cls = "normal", label = "";
    if (remaining == null) { cls = "normal"; label = ""; }
    else if (remaining <= 0) { cls = "expired"; label = L.lockExpired; }
    else if (remaining <= 30) { cls = "critical"; label = `${Math.round(remaining)}s`; }
    else if (remaining <= 120) { cls = "warning"; label = `${Math.round(remaining)}s`; }
    else { cls = "normal"; label = `${Math.round(remaining)}s`; }
    const busy = pendingOpBusy.has(o.order_id);
    const expired = remaining != null && remaining <= 0;
    return `<div class="resv-row" data-oid="${o.order_id}">
        <div class="resv-top">
          <span class="ref">${o.order_id} · ${o.ref} · ${o.qty_allocated}/${o.qty_requested}</span>
          <span class="countdown ${cls}">${label}</span>
        </div>
        <div class="resv-actions">
          <button class="ghost" data-act="confirm" ${busy || expired ? "disabled" : ""}>${L.confirm}</button>
          <button class="ghost" data-act="cancel" ${busy ? "disabled" : ""}>${L.cancel}</button>
        </div>
      </div>`;
  }).join("");

  el.querySelectorAll("[data-act]").forEach((btn) => {
    btn.onclick = async () => {
      if (btn.disabled) return;
      const oid = btn.closest("[data-oid]").dataset.oid;
      pendingOpBusy.add(oid);
      btn.closest(".resv-row").querySelectorAll("button").forEach((b) => b.disabled = true);
      try {
        await api(`/demand/${btn.dataset.act}`, { order_id: oid });
        renderLog();
      } finally {
        pendingOpBusy.delete(oid);
      }
    };
  });
}

// ---------------------------------------------------------------------------
// database health — read-only GET /api/db/check (backend/consistency.py),
// polled at a low rate since it re-scans the whole DB.
// ---------------------------------------------------------------------------
async function refreshDbHealth() {
  try {
    const r = await fetch("/api/db/check").then((x) => x.json());
    const el = $("db-health");
    const sevCls = { PASS: "on", WARN: "mode", FAIL: "off" }[r.overall] || "off";
    const sevTxt = { PASS: L.dbPass, WARN: L.dbWarn, FAIL: L.dbFail }[r.overall] || r.overall;
    el.className = "pill dbh " + sevCls;
    // the "· N checks" part is dropped first on a narrow window (CSS below)
    // -- it's the least essential part of the pill, and the top bar has
    // several other wide, un-droppable text elements competing for room.
    el.innerHTML = `● ${L.database} ${sevTxt}` +
      `<span class="dbh-detail"> · ${(r.checks || []).length} ${L.dbChecks}</span>`;
  } catch { /* DB explorer link still works even if this poll fails */ }
}

// ---------------------------------------------------------------------------
// activity log — one readable line per event instead of a wall of JSON.
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
    case "order_expired": return [p.order_id, (p.released || []).join(" + ") || null, L.orderExpired];
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
    return `<div class="ev k-${e.kind}"><span class="t">${simLabel(e.t_sim)}</span>` +
      `<span class="k">${(L.ev && L.ev[e.kind]) || e.kind}</span>` +
      `<span class="p">${bits.join(" · ")}</span></div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// connection state — CONNECTED / RECONNECTING / OFFLINE / POLLING FALLBACK
// ---------------------------------------------------------------------------
function setSysStatus(state) {
  const el = $("sys-status");
  const map = {
    online: ["", L.systemOnline], reconnecting: ["warn", L.systemReconnecting],
    offline: ["bad", L.systemOffline], polling: ["warn", L.systemPolling],
  };
  const [cls, txt] = map[state] || map.offline;
  el.className = "sysstatus" + (cls ? " " + cls : "");
  $("sys-status-txt").textContent = txt;
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------
async function refreshArticles() {
  ARTICLES = await api("/articles");
  const opts = ARTICLES.map((a) =>
    `<option value="${a.ref}">${a.ref} — ${a.label} (${a.unit_mass_g} g)</option>`).join("");
  const prevArt = $("art").value, prevDref = $("dref").value, prevFilt = $("filt").value;
  $("art").innerHTML = opts;
  $("dref").innerHTML = opts;
  $("filt").innerHTML = `<option value="*">${L.filterAll}</option>` +
    ["DRYING", "READY", "RESERVED", "QUARANTINE", "EMPTY"].map((s) =>
      `<option value="${s}">${L.st[s]}</option>`).join("") +
    ARTICLES.map((a) => `<option value="${a.ref}">${a.ref}</option>`).join("");
  if (ARTICLES.some((a) => a.ref === prevArt)) $("art").value = prevArt;
  if (ARTICLES.some((a) => a.ref === prevDref)) $("dref").value = prevDref;
  if (prevFilt === "*" || [...$("filt").options].some((o) => o.value === prevFilt)) $("filt").value = prevFilt;
  updateDemandHint();
}

// Live "N in stock" hint for the demand form, driven by the same by_ref
// totals the KPI/stock panel already shows -- this never invents a number,
// it just previews the check the backend will make anyway (fifo_allocate:
// a box is never split, so asking for more than total READY stock for the
// ref is refused outright, contract 1.3).
function updateDemandHint() {
  const ref = $("dref").value;
  const avail = refAvail[ref] || 0;
  const qty = +$("dqty").value || 0;
  const over = qty > avail;
  $("dem-avail").textContent = over ? L.demandTooMuch(avail) : L.demandAvail(avail);
  $("dem-avail").classList.toggle("over", over);
  return !over && avail > 0;
}

function openDrawer(id) { $(id).classList.add("open"); }
function closeDrawer(id) { $(id).classList.remove("open"); }

function switchView(view) {
  activeView = view;
  $("tab-rack").classList.toggle("on", view === "rack");
  $("tab-twin").classList.toggle("on", view === "twin");
  $("rackwrap").hidden = view !== "rack";
  $("twinwrap").hidden = view !== "twin";
  if (view === "twin") {
    // The canvas is inside a display:none subtree until this tab is
    // selected, so its renderer sized to 0x0 at initTwin() and twin.js
    // only re-measures on a window "resize" event -- switching tabs
    // never fires one on its own, so trigger it once the layout above
    // has actually applied (rAF, not synchronously).
    requestAnimationFrame(() => dispatchEvent(new Event("resize")));
    if (ST) updateTwin(ST);
  }
}

async function boot() {
  paintLabels();
  buildRack();
  initTwin($("cv"), $("hud"));
  switchView("rack");

  await refreshArticles();
  $("filt").onchange = () => ST && render(ST);
  $("invsearch").oninput = () => { invSearch = $("invsearch").value; if (ST) render(ST); };

  const anos = await api("/anomalies");
  $("ano").innerHTML = Object.entries(anos)
    .map(([k, v]) => `<option value="${k}">${v}</option>`).join("");

  document.querySelectorAll("[data-speed]").forEach((b) =>
    b.onclick = () => api("/clock", { speed: +b.dataset.speed }));
  $("jump6").onclick = () => api("/clock", { jump_h: 6 });
  $("btn-scenario").onclick = () =>
    api("/scenario", { name: "demo" }).then(() => { refreshArticles(); renderLog(); });

  $("btn-lang").onclick = () => {
    if (setLang(L.lang === "EN" ? "FR" : "EN")) {
      paintLabels();
      refreshArticles();
      if (ST) render(ST);
      renderLog();
      refreshDbHealth();
    }
  };

  $("tab-rack").onclick = () => switchView("rack");
  $("tab-twin").onclick = () => switchView("twin");
  $("insp-close").onclick = closeSlotInspector;

  $("btn-sim").onclick = () => openDrawer("sim-drawer");
  $("sim-close").onclick = () => closeDrawer("sim-drawer");
  $("btn-shortcuts").onclick = () => openDrawer("shortcuts-drawer");
  $("shortcuts-close").onclick = () => closeDrawer("shortcuts-drawer");
  document.querySelectorAll(".drawer-wrap").forEach((w) =>
    w.onclick = (e) => { if (e.target === w) w.classList.remove("open"); });

  const closeMenu = () => {
    $("more-pop").hidden = true;
    $("btn-more").setAttribute("aria-expanded", "false");
  };
  $("btn-more").onclick = (e) => {
    e.stopPropagation();
    const open = $("more-pop").hidden;
    if (open) {
      // menu-pop is position:fixed (see style.css) so it can't be clipped by
      // #top's horizontal-scroll safety net -- but that means its position
      // has to be computed from the button's actual on-screen rect instead
      // of via CSS anchoring.
      const r = $("btn-more").getBoundingClientRect();
      const pop = $("more-pop");
      pop.style.top = (r.bottom + 6) + "px";
      pop.style.right = Math.max(8, window.innerWidth - r.right) + "px";
    }
    $("more-pop").hidden = !open;
    $("btn-more").setAttribute("aria-expanded", String(open));
  };
  addEventListener("click", closeMenu);
  $("btn-reset").onclick = () => {
    closeMenu();
    api("/reset", {}).then(() => { refreshArticles(); renderLog(); });
  };

  const openRefModal = () => {
    closeMenu();
    $("nref-ref").value = ""; $("nref-label").value = "";
    $("nref-mass").value = ""; $("nref-cap").value = "";
    $("nref-color").value = "#22d3ee";
    $("nref-err").hidden = true;
    $("refModal").hidden = false;
    $("nref-ref").focus();
  };
  const closeRefModal = () => { $("refModal").hidden = true; };
  $("btn-newref").onclick = openRefModal;
  $("btn-nref-cancel").onclick = closeRefModal;
  $("refModal").onclick = (e) => { if (e.target === $("refModal")) closeRefModal(); };
  addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("refModal").hidden) closeRefModal();
      closeDrawer("sim-drawer"); closeDrawer("shortcuts-drawer");
      if (selectedSlot) closeSlotInspector();
    }
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

  const guardedOrderOp = (btn, fn) => {
    btn.onclick = async () => {
      if (btn.disabled || orderOpBusy) return;
      orderOpBusy = true;
      btn.disabled = true;
      try { await fn(); } finally { orderOpBusy = false; if (ST) render(ST); }
    };
  };
  $("dref").addEventListener("change", updateDemandHint);
  $("dqty").addEventListener("input", updateDemandHint);
  $("btn-demand").onclick = async () => {
    const b = $("btn-demand");
    if (b.disabled) return;
    // Client-side preview of a check the backend enforces anyway
    // (fifo_allocate refuses to split a box, contract 1.3) -- this only
    // saves a round trip; it never decides anything the backend doesn't.
    if (!updateDemandHint()) return;
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
    e.target.style.borderColor = toggleFollow() ? "#22d3ee" : "";

  connect();
  renderLog();
  refreshDbHealth();
  setInterval(renderLog, 4000);
  setInterval(refreshDbHealth, 8000);
}

let pollTimer = null;

function startPolling() {
  if (pollTimer) return;
  setSysStatus("polling");
  console.warn("[scw] WebSocket unavailable - falling back to polling");
  pollTimer = setInterval(() => api("/state").then(render).catch(() => setSysStatus("offline")), 500);
}

function connect() {
  let ws;
  setSysStatus("reconnecting");
  try {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  } catch (e) { return startPolling(); }
  const guard = setTimeout(startPolling, 2500);   // never opened -> poll
  ws.onopen = () => {
    clearTimeout(guard);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    setSysStatus("online");
  };
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onerror = () => startPolling();
  ws.onclose = () => { setSysStatus("reconnecting"); setTimeout(connect, 1000); };
}

// --- demo hotkeys: rehearse these, they are faster than hunting for buttons --
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "?") { e.preventDefault(); openDrawer("shortcuts-drawer"); return; }
  const map = {
    a: () => $("btn-arrive").click(),
    d: () => $("btn-demand").click(),
    c: () => !$("btn-confirm").disabled && $("btn-confirm").click(),
    j: () => api("/clock", { jump_h: 6 }),
    r: () => $("btn-reset").click(),
    s: () => $("btn-scenario").click(),
    1: () => { switchView("twin"); setCamera("iso"); },
    2: () => { switchView("twin"); setCamera("aisle"); },
    3: () => { switchView("twin"); setCamera("front"); },
    4: () => { switchView("twin"); setCamera("top"); },
  };
  if (map[e.key]) { e.preventDefault(); map[e.key](); }
});

boot();
