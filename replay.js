// replay.js — GitHub Pages mode: the dashboard with no backend anywhere.
//
// tools/build_pages.py sets window.SCW_REPLAY to a recording that
// tools/record_replay.py made against a real backend: every /ws state
// snapshot (as top-level-key patches) plus the GET answers the page polls.
// This plays it through the same render() the WebSocket feeds, answers GETs
// from the recording at the current playback time, and refuses every POST --
// the page is read-only and never sends anything anywhere. Served by the
// backend (window.SCW_REPLAY unset), none of this runs.

import { L } from "./labels.js";

export const REPLAY = window.SCW_REPLAY || null;

const HOLD_S = 4;            // pause on the last frame before looping
let recP = null;
let states = [];             // [t, full snapshot]
let cur = 0;                 // playback position, seconds

const load = () => recP || (recP = fetch(REPLAY).then((r) => r.json()));
const fr = () => L.lang === "FR";
const title = () => (fr() ? "Démo enregistrée · lecture seule" : "Recorded demo · read-only");
const readOnly = () => (fr()
  ? "Démo enregistrée (GitHub Pages) : lecture seule. Lancez ./run.sh pour piloter le système réel."
  : "Recorded demo (GitHub Pages): read-only. Run ./run.sh to drive the live system.");

// latest entry of a [t, value] series at or before t
function at(series, t) {
  let v = series[0][1];
  for (const [ts, val] of series) {
    if (ts > t) break;
    v = val;
  }
  return v;
}

export async function replayApi(path, body) {
  const rec = await load();
  if (body !== undefined) return { error: readOnly() };
  if (path === "/state") return states.length ? at(states, cur) : {};
  const series = rec.gets[path];
  if (series && series.length) return at(series, cur);
  return path.startsWith("/events") || path.startsWith("/barcodes") ? [] : {};
}

export async function startReplay(render, setSysStatus) {
  const rec = await load();
  let acc = {};
  states = rec.frames.map(([t, patch]) => [t, (acc = { ...acc, ...patch })]);
  const dur = rec.meta.duration_s;
  const ui = buildBar(dur);
  let origin = performance.now(), paused = false, shown = null;

  const seek = (t) => {
    cur = Math.max(0, Math.min(dur, t));
    origin = performance.now() - cur * 1000;
  };
  ui.play.onclick = () => { paused = !paused; if (!paused) seek(cur); ui.sync(paused); };
  ui.restart.onclick = () => seek(0);
  ui.range.oninput = () => seek(+ui.range.value);
  ui.sync(false);

  const tick = () => {
    if (!paused) {
      cur = (performance.now() - origin) / 1000;
      if (cur > dur + HOLD_S) seek(0);
    }
    const st = at(states, Math.min(cur, dur));
    if (st !== shown) { shown = st; render(st); }
    setSysStatus("online");
    document.getElementById("sys-status-txt").textContent = fr() ? "Démo enregistrée" : "Recorded demo";
    ui.update(Math.min(cur, dur));
  };
  tick();
  setInterval(tick, 100);
}

function buildBar(dur) {
  const css = document.createElement("style");
  css.textContent = `
#replaybar{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:60;
  display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  background:rgba(13,18,26,.94);border:1px solid #2b3645;color:#e6edf3;
  font:12px/1.2 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.35);
  max-width:calc(100vw - 32px)}
#replaybar b{color:#22d3ee;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#replaybar button{background:none;border:1px solid #2b3645;color:inherit;border-radius:6px;
  padding:2px 8px;cursor:pointer;min-width:30px}
#replaybar input{width:min(320px,34vw)}
#replaybar .t{font-variant-numeric:tabular-nums;white-space:nowrap;opacity:.8}`;
  document.head.appendChild(css);

  const bar = document.createElement("div");
  bar.id = "replaybar";
  bar.innerHTML = `<b></b><button data-k="play"></button><button data-k="restart">&#10226;</button>` +
    `<input type="range" min="0" max="${dur}" step="0.1" value="0"><span class="t"></span>`;
  document.body.appendChild(bar);

  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const label = bar.querySelector("b"), time = bar.querySelector(".t");
  const range = bar.querySelector("input");
  return {
    play: bar.querySelector('[data-k="play"]'),
    restart: bar.querySelector('[data-k="restart"]'),
    range,
    sync(p) { this.play.textContent = p ? "▶" : "❚❚"; },
    update(t) {
      label.textContent = title();
      if (document.activeElement !== range) range.value = t;
      time.textContent = `${mmss(t)} / ${mmss(dur)}`;
    },
  };
}
