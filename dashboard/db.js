// db.js — Database Explorer. Vanilla JS, no build step, same origin as the
// backend (no CORS). Every request here hits a read-only /api/db/* endpoint.

const $ = (id) => document.getElementById(id);
const api = (path) => fetch("/api/db" + path).then((r) => r.json());

// Reused everywhere identity needs a color: same hex the live dashboard uses
// for box state (see dashboard/style.css .s-STATE and twin.js COLOR), so a
// state means the same thing on both pages.
const STATE_COLOR = {
  READY: "#22c98a", DRYING: "#f5a623", RESERVED: "#7aa2ff",
  QUARANTINE: "#ff5d5d", PICKING: "#b39bff", STORING: "#b39bff",
  INCOMING: "#b39bff", IDENTIFYING: "#b39bff", COUNTING: "#b39bff",
  EMPTY: "#5b6673", ARCHIVED: "#5b6673",
};
const STATE_LABEL = {
  READY: "Ready", DRYING: "Drying", RESERVED: "Reserved",
  QUARANTINE: "Quarantine", PICKING: "Picking", STORING: "Storing",
  INCOMING: "Incoming", IDENTIFYING: "Identifying", COUNTING: "Counting",
  EMPTY: "Empty", ARCHIVED: "Archived",
};

// ---------------------------------------------------------------------------
// shared tooltip (hover layer — see dataviz interaction.md)
// ---------------------------------------------------------------------------
const tip = $("tooltip");
function showTip(evt, colorHex, label, value) {
  tip.innerHTML = "";
  const v = document.createElement("div");
  v.className = "t-val";
  v.textContent = value;
  const l = document.createElement("div");
  l.className = "t-lbl";
  if (colorHex) {
    const key = document.createElement("span");
    key.className = "t-key";
    key.style.background = colorHex;
    l.appendChild(key);
  }
  l.appendChild(document.createTextNode(label));
  tip.appendChild(v);
  tip.appendChild(l);
  tip.hidden = false;
  moveTip(evt);
}
function moveTip(evt) {
  const pad = 14;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + 220 > innerWidth) x = evt.clientX - 220 - pad;
  if (y + 60 > innerHeight) y = evt.clientY - 60 - pad;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hideTip() { tip.hidden = true; }

// ---------------------------------------------------------------------------
// KPI strip
// ---------------------------------------------------------------------------
function renderKpis(tablesInfo, stats) {
  const totalRows = tablesInfo.reduce((s, t) => s + t.row_count, 0);
  const coresInStock = Object.entries(stats.by_state || {})
    .filter(([s]) => s === "READY" || s === "DRYING" || s === "RESERVED");
  const cores = (stats.by_ref || []).reduce(
    (s, r) => s + r.ready + r.drying + r.reserved, 0);
  const tiles = [
    [tablesInfo.length, "Tables"],
    [totalRows.toLocaleString(), "Total rows"],
    [(stats.by_state.QUARANTINE || 0), "Boxes in quarantine"],
    [cores.toLocaleString(), "Cores in stock"],
    [stats.events_total.toLocaleString(), "Events logged"],
  ];
  $("kpis").innerHTML = tiles.map(([v, k]) =>
    `<div class="tile"><div class="v">${v}</div><div class="k">${k}</div></div>`
  ).join("");
}

// ---------------------------------------------------------------------------
// ERD — hand-laid-out (5 known tables), driven by the live schema response
// so column lists never drift from reality.
// ---------------------------------------------------------------------------
const ERD_LAYOUT = {
  articles: { x: 20, y: 20, w: 190 },
  boxes: { x: 300, y: 10, w: 220 },
  slots: { x: 600, y: 20, w: 190 },
  orders: { x: 300, y: 300, w: 220 },
  events: { x: 600, y: 300, w: 190 },
};
const MAX_COLS_SHOWN = 6;

function tableBox(t) {
  const pos = ERD_LAYOUT[t.name];
  const shown = t.columns.slice(0, MAX_COLS_SHOWN);
  const extra = t.columns.length - shown.length;
  const rowH = 15, headH = 22, pad = 8;
  const h = headH + shown.length * rowH + (extra > 0 ? rowH : 0) + pad;
  pos.h = h;
  const rows = shown.map((c, i) => {
    const y = headH + i * rowH + 11;
    const cls = c.pk ? "pk" : "col";
    const suffix = c.pk ? " (pk)" : "";
    return `<text x="${pos.x + 10}" y="${pos.y + y}" class="${cls}" font-size="10.5">${esc(c.name)}${suffix}</text>
            <text x="${pos.x + pos.w - 10}" y="${pos.y + y}" class="col" font-size="9.5" text-anchor="end">${esc(c.type || "")}</text>`;
  }).join("");
  const more = extra > 0
    ? `<text x="${pos.x + 10}" y="${pos.y + headH + shown.length * rowH + 11}" class="col" font-size="9.5" font-style="italic">+${extra} more…</text>`
    : "";
  return `<g class="erd-table">
    <rect x="${pos.x}" y="${pos.y}" width="${pos.w}" height="${h}" rx="8"/>
    <text x="${pos.x + 10}" y="${pos.y + 15}" class="title" font-size="12">${esc(t.name)}</text>
    <text x="${pos.x + pos.w - 10}" y="${pos.y + 15}" class="col" font-size="9.5" text-anchor="end">${t.row_count} rows</text>
    ${rows}${more}
  </g>`;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function edgeBetween(fromName, toName, kind, opts) {
  opts = opts || {};
  const a = ERD_LAYOUT[fromName], b = ERD_LAYOUT[toName];
  if (!a || !b) return "";
  const fromBottom = opts.vertical === "down";
  const cls = kind === "fk" ? "erd-edge" : "erd-edge " + kind;
  if (fromBottom) {
    const ax = a.x + (opts.xFrac || 0.5) * a.w, ay = a.y + a.h;
    const bx = b.x + (opts.xFracTo || 0.5) * b.w, by = b.y;
    const my = (ay + by) / 2;
    return `<path class="${cls}" d="M${ax},${ay} C${ax},${my} ${bx},${my} ${bx},${by}" marker-end="url(#arrow)"/>`;
  }
  const ax = a.x < b.x ? a.x + a.w : a.x;
  const bx = a.x < b.x ? b.x : b.x + b.w;
  const ay = a.y + (opts.yFrom || 20), by = b.y + (opts.yTo || 20);
  const mx = (ax + bx) / 2;
  return `<path class="${cls}" d="M${ax},${ay} C${mx},${ay} ${mx},${by} ${bx},${by}" marker-end="url(#arrow)"/>`;
}

function renderErd(schema) {
  const byName = Object.fromEntries(schema.tables.map((t) => [t.name, t]));
  const order = ["articles", "boxes", "slots", "orders", "events"];
  const boxesSvg = order.map((n) => tableBox(byName[n])).join("");
  const edges = [
    edgeBetween("boxes", "articles", "fk"),
    edgeBetween("boxes", "slots", "fk"),
    edgeBetween("slots", "orders", "soft", { vertical: "down", xFrac: 0.25, xFracTo: 0.85 }),
    edgeBetween("orders", "boxes", "json", { yFrom: 34, yTo: 34 }),
  ].join("");
  const maxY = Math.max(...order.map((n) => ERD_LAYOUT[n].y + ERD_LAYOUT[n].h)) + 20;
  $("erd").innerHTML = `<svg viewBox="0 0 820 ${maxY}">
    <defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path class="erd-arrow" d="M0,0 L8,4 L0,8 z"/></marker></defs>
    ${edges}${boxesSvg}
  </svg>`;
}

function renderRelations(relations) {
  const TAG = { fk: "FK", soft: "SOFT", json: "JSON" };
  $("rels").innerHTML = relations.map((r) => `
    <div class="rel">
      <span class="tag ${r.kind}">${TAG[r.kind]}</span>
      <b>${esc(r.from)}</b><span class="arrow">→</span><b>${esc(r.to)}</b>
      <span class="note">${esc(r.note)}</span>
    </div>`).join("");
}

// ---------------------------------------------------------------------------
// bar charts — one hue (status/entity) per row, direct labels (mandatory:
// 4+ categorical series), legend, hover tooltip. See references/color-formula.md.
// ---------------------------------------------------------------------------
function barChart(el, rows, opts) {
  // rows: [{label, value, color}]; opts.max optional
  if (!rows.length) { el.innerHTML = `<div class="empty">No data yet.</div>`; return; }
  const max = opts.max || Math.max(1, ...rows.map((r) => r.value));
  el.innerHTML = "";
  rows.forEach((r) => {
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = r.label;
    const track = document.createElement("div");
    track.className = "track";
    const fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = Math.max(2, (r.value / max) * 100) + "%";
    fill.style.background = r.color;
    track.appendChild(fill);
    const val = document.createElement("div");
    val.className = "val";
    val.textContent = r.value.toLocaleString();
    row.appendChild(name); row.appendChild(track); row.appendChild(val);
    el.appendChild(row);
    const onmove = (e) => showTip(e, r.color, r.label, r.value.toLocaleString());
    fill.addEventListener("pointermove", onmove);
    fill.addEventListener("pointerenter", onmove);
    fill.addEventListener("pointerleave", hideTip);
  });
}

function renderChartState(stats) {
  const order = ["READY", "DRYING", "RESERVED", "QUARANTINE", "EMPTY", "ARCHIVED"];
  const rows = order.filter((s) => stats.by_state[s])
    .map((s) => ({ label: STATE_LABEL[s], value: stats.by_state[s], color: STATE_COLOR[s] }));
  barChart($("chartState"), rows, {});
}

function renderChartRef(stats) {
  const rows = (stats.by_ref || []).map((r) => ({
    label: r.ref, value: r.ready + r.drying + r.reserved,
    color: r.color || "#4f8cff",
  })).filter((r) => r.value > 0 || true);
  barChart($("chartRef"), rows, {});
}

// ---------------------------------------------------------------------------
// rack occupancy grid — the literal content of `slots`, drawn in the same
// face/column/level shape as the physical room, colored by occupant state.
// ---------------------------------------------------------------------------
function renderGrid(stats) {
  const cells = stats.grid;
  if (!cells.length) { $("rackgrid").innerHTML = `<div class="empty">No slots.</div>`; return; }
  const faces = [...new Set(cells.map((c) => c.face))].sort();
  const cols = [...new Set(cells.map((c) => c.col))].sort((a, b) => a - b);
  const levels = [...new Set(cells.map((c) => c.level))].sort((a, b) => a - b);
  $("gridSub").textContent = `${faces.length} faces × ${cols.length} columns × ${levels.length} levels = ${cells.length} slots`;

  const legendStates = ["READY", "DRYING", "RESERVED", "QUARANTINE"];
  $("gridLegend").innerHTML = legendStates.map((s) =>
    `<span><span class="sw" style="background:${STATE_COLOR[s]}"></span>${STATE_LABEL[s]}</span>`
  ).join("") + `<span><span class="sw" style="background:#232b35;border:1px solid #26303d"></span>Empty slot</span>`;

  $("rackgrid").innerHTML = "";
  faces.forEach((f) => {
    const faceDiv = document.createElement("div");
    faceDiv.className = "face";
    const label = document.createElement("div");
    label.className = "facelabel";
    label.textContent = "Face " + f;
    faceDiv.appendChild(label);
    levels.forEach((lvl) => {
      const rowDiv = document.createElement("div");
      rowDiv.className = "levelrow";
      cols.forEach((col) => {
        const cell = cells.find((c) => c.face === f && c.col === col && c.level === lvl);
        const div = document.createElement("div");
        div.className = "cell";
        if (cell && cell.box_id) {
          div.style.background = STATE_COLOR[cell.state] || "#888";
        }
        const onmove = (e) => {
          if (cell && cell.box_id) {
            showTip(e, STATE_COLOR[cell.state],
              `${cell.slot_id} · ${cell.box_id} · ${cell.article_ref}`,
              `${cell.qty_available} cores · ${STATE_LABEL[cell.state] || cell.state}`);
          } else {
            showTip(e, null, cell ? cell.slot_id : "", "empty");
          }
        };
        div.addEventListener("pointermove", onmove);
        div.addEventListener("pointerenter", onmove);
        div.addEventListener("pointerleave", hideTip);
        rowDiv.appendChild(div);
      });
      faceDiv.appendChild(rowDiv);
    });
    $("rackgrid").appendChild(faceDiv);
  });
}

// ---------------------------------------------------------------------------
// table browser — the accessibility "table view" for every chart above,
// and the plain way to look at what a row actually contains.
// ---------------------------------------------------------------------------
let TB = { name: "boxes", limit: 50, offset: 0, q: "" };

async function loadTable() {
  const data = await api(`/table/${TB.name}?limit=${TB.limit}&offset=${TB.offset}&q=${encodeURIComponent(TB.q)}`);
  const thead = $("datatable").tHead;
  thead.innerHTML = "";
  const trh = document.createElement("tr");
  data.columns.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    trh.appendChild(th);
  });
  thead.appendChild(trh);

  const tbody = $("datatable").tBodies[0];
  tbody.innerHTML = "";
  data.rows.forEach((row) => {
    const tr = document.createElement("tr");
    data.columns.forEach((c) => {
      const td = document.createElement("td");
      const v = row[c];
      if (v === null || v === undefined) {
        td.className = "null";
        td.textContent = "null";
      } else {
        td.textContent = typeof v === "number" ? v : String(v);
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  const from = data.total === 0 ? 0 : TB.offset + 1;
  const to = Math.min(TB.offset + TB.limit, data.total);
  $("tblcount").textContent = `${from}–${to} of ${data.total}`;
  $("prev").disabled = TB.offset === 0;
  $("next").disabled = to >= data.total;
}

function wireTableBrowser(tableNames) {
  $("tbl").innerHTML = tableNames.map((t) =>
    `<option value="${t.name}">${t.name} (${t.row_count})</option>`).join("");
  $("tbl").value = TB.name;
  $("tbl").onchange = () => { TB.name = $("tbl").value; TB.offset = 0; loadTable(); };
  $("q").oninput = debounce(() => { TB.offset = 0; TB.q = $("q").value; loadTable(); }, 250);
  $("prev").onclick = () => { TB.offset = Math.max(0, TB.offset - TB.limit); loadTable(); };
  $("next").onclick = () => { TB.offset += TB.limit; loadTable(); };
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ---------------------------------------------------------------------------
// boot + polling
// ---------------------------------------------------------------------------
let schemaCache = null;

async function refreshAll() {
  const [tablesInfo, stats] = await Promise.all([api("/tables"), api("/stats")]);
  renderKpis(tablesInfo, stats);
  renderChartState(stats);
  renderChartRef(stats);
  renderGrid(stats);
  if (TB.name) loadTable();
}

async function boot() {
  const schema = await api("/schema");
  schemaCache = schema;
  renderErd(schema);
  renderRelations(schema.relations);
  wireTableBrowser(schema.tables);
  await refreshAll();

  $("btn-refresh").onclick = refreshAll;
  setInterval(() => { if ($("live").checked) refreshAll(); }, 3000);

  addEventListener("pointermove", () => {}, { passive: true }); // keep tip mount warm
}

boot();
