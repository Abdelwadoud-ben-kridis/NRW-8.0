// twin.js — the cosmetic 3D digital twin, part of the live dashboard
// (criterion 8). It is NOT criterion 7 (mechanical design + animated 3D),
// which is a separate CAD deliverable -- see README §1. Everything here is
// generated procedurally from the slots table; only the crane, the fork, the
// crate and the conveyor are optional .glb files, and the scene falls back to
// primitives the moment one is missing.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// --- room + rack geometry, in metres. Must match backend/config.py ----------
export const GEO = {
  room: 6.0,
  faces: 2, cols: 9, levels: 17,
  rackX: 5.6, rackZ: 4.8,          // rackZ = usable height (Three's Y)
  aisle: 1.3,                       // face offset from the aisle centreline
  crate: [0.515, 0.175, 0.325],   // 51.5 x 32.5 x 17.5 cm (L x H x W)
};
GEO.colW = GEO.rackX / GEO.cols;
GEO.lvlH = GEO.rackZ / GEO.levels;

// exported so app.js can build the on-screen legend from the SAME hexes the
// scene paints with -- a legend that can drift from the scene is worse than none
export const COLOR = {
  DRYING: 0xf5a623, READY: 0x22c98a, RESERVED: 0x4f8cff,
  QUARANTINE: 0xff5d5d, PICKING: 0xb39bff, STORING: 0xb39bff,
  EMPTY: 0x3a4453, ARCHIVED: 0x3a4453, INCOMING: 0xb39bff,
  IDENTIFYING: 0xb39bff, COUNTING: 0xb39bff,
};

let scene, camera, renderer, controls, raf;
let crateGroup, crane = {}, conveyor, crateProto = null;
const crates = new Map();           // box_id -> Mesh
let follow = false;
let hudEl = null;

// The rail overhangs the rack by 0.3 m on each side (see buildCrane) -- the
// left overhang is the conveyor hand-off point. Every crate travels between
// the rack and the conveyor riding the fork; nothing ever drifts on its own.
const CONVEYOR_X = -(GEO.rackX / 2 + 0.3);
const CONVEYOR_Y = 0.4;

let jobQueue = [];      // queued {steps:[{x,y,fork,onArrive?,holdMs?}], i, waitUntil}
let job = null;         // the job currently being animated
let craneSeq = -1;

// ---------------------------------------------------------------------------
export function slotPosition(slotId) {
  // "F0-C3-L7" -> world position of that slot's crate centre
  const m = /^F(\d+)-C(\d+)-L(\d+)$/.exec(slotId || "");
  if (!m) return null;
  const [, f, c, l] = m.map(Number);
  return new THREE.Vector3(
    -GEO.rackX / 2 + (c - 0.5) * GEO.colW,
    (l - 0.5) * GEO.lvlH + 0.06,
    (f === 0 ? -1 : 1) * GEO.aisle
  );
}

// ---------------------------------------------------------------------------
export function initTwin(canvas, hud) {
  hudEl = hud;
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x090d12);
  scene.fog = new THREE.Fog(0x090d12, 14, 30);

  camera = new THREE.PerspectiveCamera(45, 1, 0.1, 200);
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.target.set(0, 2.0, 0);

  // lights
  scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x1b2430, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.5);
  key.position.set(6, 9, 5);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x88aaff, 0.5);
  fill.position.set(-6, 4, -5);
  scene.add(fill);

  buildRoom();
  buildRacks();
  buildCrane();
  buildConveyor();

  crateGroup = new THREE.Group();
  scene.add(crateGroup);

  loadOptionalModels();
  setCamera("iso");
  onResize();
  addEventListener("resize", onResize);
  // The dashboard's panels are draggable (app.js::setupResizeGutters), which
  // resizes this canvas WITHOUT a window resize event -- the drawing buffer
  // then kept its old size and the scene rendered stretched. Watch the
  // canvas itself.
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(onResize).observe(canvas);
  animate();
}

function onResize() {
  const c = renderer.domElement;
  if (!c.clientWidth || !c.clientHeight) return;   // hidden tab: keep last size
  const w = c.clientWidth, h = c.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

// --- static scene ----------------------------------------------------------

function buildRoom() {
  const R = GEO.room;
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(R, R),
    new THREE.MeshStandardMaterial({ color: 0x161c25, roughness: 0.95 }));
  floor.rotation.x = -Math.PI / 2;
  scene.add(floor);

  const grid = new THREE.GridHelper(R, 12, 0x2a3542, 0x1d2531);
  grid.position.y = 0.002;
  scene.add(grid);

  // the 6 x 6 x 6 m envelope, drawn so the jury can see we used it
  const box = new THREE.Box3(
    new THREE.Vector3(-R / 2, 0, -R / 2), new THREE.Vector3(R / 2, R, R / 2));
  const helper = new THREE.Box3Helper(box, 0x2f3d4d);
  scene.add(helper);
}

function buildRacks() {
  const post = new THREE.MeshStandardMaterial({ color: 0x39465a, roughness: 0.6, metalness: 0.35 });
  const beam = new THREE.MeshStandardMaterial({ color: 0x2c3849, roughness: 0.7, metalness: 0.3 });

  for (let f = 0; f < GEO.faces; f++) {
    const z = (f === 0 ? -1 : 1) * GEO.aisle;
    const g = new THREE.Group();

    for (let c = 0; c <= GEO.cols; c++) {                 // uprights
      const x = -GEO.rackX / 2 + c * GEO.colW;
      const m = new THREE.Mesh(new THREE.BoxGeometry(0.06, GEO.rackZ, 0.06), post);
      m.position.set(x, GEO.rackZ / 2, z - 0.18);
      g.add(m);
      const m2 = m.clone();
      m2.position.z = z + 0.18;
      g.add(m2);
    }
    for (let l = 0; l <= GEO.levels; l++) {               // shelves
      const y = l * GEO.lvlH;
      const m = new THREE.Mesh(
        new THREE.BoxGeometry(GEO.rackX, 0.02, 0.4), beam);
      m.position.set(0, y, z);
      // 3 deg seating incline: repeatable seating against a cushioned stop,
      // NOT a FIFO mechanism -- FIFO is enforced entirely in software.
      m.rotation.x = (f === 0 ? 1 : -1) * THREE.MathUtils.degToRad(3);
      g.add(m);
    }
    scene.add(g);
  }
}

function buildCrane() {
  const mat = new THREE.MeshStandardMaterial({ color: 0x5b7aa8, metalness: 0.6, roughness: 0.35 });
  const g = new THREE.Group();

  const rail = new THREE.Mesh(
    new THREE.BoxGeometry(GEO.rackX + 0.6, 0.05, 0.12),
    new THREE.MeshStandardMaterial({ color: 0x2a3542 }));
  rail.position.set(0, 0.025, 0);
  scene.add(rail);

  crane.mast = new THREE.Mesh(new THREE.BoxGeometry(0.14, GEO.rackZ + 0.5, 0.14), mat);
  crane.mast.position.y = (GEO.rackZ + 0.5) / 2;
  g.add(crane.mast);

  crane.carriage = new THREE.Group();
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.22, 0.5), mat);
  crane.carriage.add(body);

  crane.fork = new THREE.Group();
  const f1 = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.035, 0.06), mat);
  f1.position.set(0, -0.1, -0.12);
  const f2 = f1.clone(); f2.position.z = 0.12;
  crane.fork.add(f1, f2);
  crane.carriage.add(crane.fork);

  crane.held = new THREE.Group();          // crate riding on the fork
  crane.fork.add(crane.held);

  crane.carriage.position.y = CONVEYOR_Y;
  g.add(crane.carriage);

  crane.root = g;
  g.position.x = CONVEYOR_X;     // parked at the conveyor hand-off, not mid-rack
  scene.add(g);
}

function buildConveyor() {
  const g = new THREE.Group();
  const belt = new THREE.Mesh(
    new THREE.BoxGeometry(1.6, 0.08, 0.6),
    new THREE.MeshStandardMaterial({ color: 0x2b3644, roughness: 0.85 }));
  belt.position.y = 0.45;
  g.add(belt);
  for (const x of [-0.7, 0.7]) {
    for (const z of [-0.25, 0.25]) {
      const leg = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.45, 0.05),
        new THREE.MeshStandardMaterial({ color: 0x222b36 }));
      leg.position.set(x, 0.22, z);
      g.add(leg);
    }
  }
  // photoelectric barrier: emitter + receiver + the beam itself
  const pole = new THREE.MeshStandardMaterial({ color: 0x1d2531 });
  for (const z of [-0.38, 0.38]) {
    const p = new THREE.Mesh(new THREE.BoxGeometry(0.07, 0.55, 0.07), pole);
    p.position.set(0.35, 0.75, z);
    g.add(p);
  }
  const beamGeo = new THREE.CylinderGeometry(0.008, 0.008, 0.76, 8);
  conveyorBeam = new THREE.Mesh(beamGeo, new THREE.MeshBasicMaterial({
    color: 0xff3b3b, transparent: true, opacity: 0.75 }));
  conveyorBeam.rotation.x = Math.PI / 2;
  conveyorBeam.position.set(0.35, 0.92, 0);
  g.add(conveyorBeam);

  g.position.set(-GEO.rackX / 2 - 1.0, 0, 0);
  conveyor = g;
  scene.add(g);
}
let conveyorBeam;

// --- optional GLB parts (P1's Fusion exports) ------------------------------

function loadOptionalModels() {
  const loader = new GLTFLoader();
  const tryLoad = (file, onOk) =>
    loader.load(new URL("models/" + file, import.meta.url).href,   // /static/models/ or Pages
      (gltf) => { try { onOk(gltf.scene); console.log("[twin] loaded", file); }
                  catch (e) { console.warn(e); } },
      undefined,
      () => console.log("[twin] no", file, "- using the primitive"));

  tryLoad("crate.glb", (obj) => { crateProto = obj; refreshAllCrateMeshes(); });
  tryLoad("crane.glb", (obj) => {
    crane.mast.visible = false;
    obj.position.y = 0;
    crane.root.add(obj);
  });
  tryLoad("fork.glb", (obj) => {
    crane.fork.children.forEach((c) => { if (c !== crane.held) c.visible = false; });
    crane.fork.add(obj);
  });
  tryLoad("conveyor.glb", (obj) => {
    conveyor.children.forEach((c) => { if (c !== conveyorBeam) c.visible = false; });
    conveyor.add(obj);
  });
}

function newCrateMesh(color) {
  if (crateProto) {
    const o = crateProto.clone(true);
    o.traverse((n) => {
      if (n.isMesh) n.material = new THREE.MeshStandardMaterial(
        { color, roughness: 0.75 });
    });
    return o;
  }
  const g = new THREE.Group();
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(...GEO.crate),
    new THREE.MeshStandardMaterial({ color, roughness: 0.7, metalness: 0.05 }));
  const edge = new THREE.LineSegments(
    new THREE.EdgesGeometry(body.geometry),
    new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35 }));
  g.add(body, edge);
  g.userData.body = body;
  return g;
}

function refreshAllCrateMeshes() {
  for (const [id, mesh] of crates) { crateGroup.remove(mesh); crates.delete(id); }
}

function tint(mesh, color) {
  mesh.traverse((n) => { if (n.isMesh && n.material && n.material.color)
    n.material.color.setHex(color); });
}

// --- crane jobs --------------------------------------------------------
// A job is a short list of {x, y, fork, onArrive?, holdMs?} steps that the
// crane's carriage/fork are eased toward, one at a time (see animate()).
// The crate mesh itself never has its own opinion about where it is while
// a crane is handling it -- it rides inside crane.held, which THREE keeps
// attached to the fork tip, and is only handed back to crateGroup (at its
// exact world position, via .attach()) when the job says it has arrived.

function queueStoreJob(boxId, slotPos) {
  const m = crates.get(boxId);
  if (!m) return;
  const faceSign = Math.sign(slotPos.z) || 1;
  const reach = faceSign * GEO.aisle;   // fork tip lands exactly on the slot's Z
  jobQueue.push({
    i: 0, waitUntil: null,
    steps: [
      // 1. go to the conveyor and pick the new crate up off the belt
      { x: CONVEYOR_X, y: CONVEYOR_Y, fork: 0, onArrive: () => {
          m.userData.pendingJob = false;
          m.userData.riding = true;
          crane.held.attach(m);
        } },
      // 2. carry it to the right column/level
      { x: slotPos.x, y: slotPos.y, fork: 0 },
      // 3. extend the fork into the rack face and set the crate down
      { x: slotPos.x, y: slotPos.y, fork: reach, holdMs: 150, onArrive: () => {
          crateGroup.attach(m);
          m.position.copy(slotPos);
          m.userData.riding = false;
          m.userData.goal = slotPos;
        } },
      // 4. retract, then head back to the conveyor for the next job
      { x: slotPos.x, y: slotPos.y, fork: 0 },
      { x: CONVEYOR_X, y: CONVEYOR_Y, fork: 0 },
    ],
  });
}

function queuePickJob(boxId, slotPos) {
  const m = crates.get(boxId);
  if (!m) return;
  const faceSign = Math.sign(slotPos.z) || 1;
  const reach = faceSign * GEO.aisle;
  jobQueue.push({
    i: 0, waitUntil: null,
    steps: [
      // 1. go to the box's column/level
      { x: slotPos.x, y: slotPos.y, fork: 0 },
      // 2. extend into the rack face and grab it
      { x: slotPos.x, y: slotPos.y, fork: reach, holdMs: 150, onArrive: () => {
          m.userData.riding = true;
          crane.held.attach(m);
        } },
      // 3. retract -- the crate rides along, still on the fork
      { x: slotPos.x, y: slotPos.y, fork: 0 },
      // 4. carry it out to the conveyor and hand it off to production
      { x: CONVEYOR_X, y: CONVEYOR_Y, fork: 0, onArrive: () => {
          crateGroup.attach(m);
          m.userData.riding = false;
          // Next state update decides what happens to it: a partial pick
          // still owns its old slot, so it eases back there on its own;
          // a box the backend has now emptied simply won't be in `seen`
          // any more and gets cleaned up below.
        } },
    ],
  });
}

// --- per-frame state -------------------------------------------------------

let lastState = null;

export function updateTwin(st) {
  lastState = st;
  const seen = new Set();

  for (const b of st.boxes) {
    if (!b.slot_id) continue;
    seen.add(b.box_id);
    let m = crates.get(b.box_id);
    const color = COLOR[b.state] ?? 0x888888;
    const p = slotPosition(b.slot_id);
    if (!m) {
      m = newCrateMesh(color);
      crates.set(b.box_id, m);
      crateGroup.add(m);
      // A brand-new box waits at the conveyor -- the crane job started
      // below carries it to the slot. It never teleports or free-floats.
      m.position.set(CONVEYOR_X, CONVEYOR_Y, 0);
      m.userData.pendingJob = true;
      m.userData.pendingSince = performance.now();
    }
    m.userData.pendingGoal = p;
    if (!m.userData.riding && !m.userData.pendingJob) m.userData.goal = p;
    tint(m, color);
    m.userData.state = b.state;
  }
  for (const [id, m] of [...crates]) {
    if (!seen.has(id)) { crateGroup.remove(m); crates.delete(id); }
  }

  // a new crane command: queue the matching job (store carries a fresh
  // crate in from the conveyor; pick carries an existing one back out)
  if (st.crane && st.crane.seq !== craneSeq) {
    craneSeq = st.crane.seq;
    const p = slotPosition(st.crane.slot_id);
    if (p && st.crane.cmd === "store") queueStoreJob(st.crane.box_id, p);
    else if (p && st.crane.cmd === "pick") queuePickJob(st.crane.box_id, p);
  }
  if (st.device) {
    const obstructed = st.device.state === "COUNTING" && !st.device.stable;
    if (conveyorBeam) conveyorBeam.material.color.setHex(
      obstructed ? 0x22c98a : 0xff3b3b);
  }
}

export function setCamera(name) {
  const views = {
    iso:   [7.5, 5.5, 7.5, 0, 2.0, 0],
    aisle: [-4.6, 1.6, 0.02, 3.2, 2.0, 0],
    front: [0, 2.6, 9.0, 0, 2.4, 0],
    top:   [0.01, 11.5, 0.01, 0, 0, 0],
  };
  const v = views[name] || views.iso;
  camera.position.set(v[0], v[1], v[2]);
  controls.target.set(v[3], v[4], v[5]);
  controls.update();
}

export function toggleFollow() { follow = !follow; return follow; }

// --- animation loop --------------------------------------------------------

let lastFrameT = null;

function animate(now = performance.now()) {
  raf = requestAnimationFrame(animate);
  // Nothing to draw while the 3D tab is hidden (the 2D rack is the default
  // view) -- don't burn the GPU behind it.
  if (!renderer.domElement.clientWidth) { lastFrameT = null; return; }

  // Real elapsed time, not a fixed 1/60 s: at a fixed step every crate and
  // the crane moved at (fps / 60) of their real speed -- at the ~13 fps a
  // busy dashboard can drop to, crates took ~30 s to reach their slots and
  // the twin looked frozen/empty. Clamped so a stalled frame can't teleport.
  const dt = lastFrameT == null ? 1 / 60 : Math.min(0.1, Math.max(0, (now - lastFrameT) / 1000));
  lastFrameT = now;

  // crane: X travel 1.2 m/s, Z lift 0.8 m/s, ease-out at both ends
  // (cores are fragile before curing -- gentle handling is a design driver)
  const g = crane.root;
  const ease = (cur, goal, vmax) => {
    const d = goal - cur;
    const ad = Math.abs(d);
    if (ad < 0.002) return goal;
    const v = Math.min(vmax, vmax * Math.min(1, ad / 0.6) + 0.05);
    return cur + Math.sign(d) * Math.min(ad, v * dt);
  };

  if (!job && jobQueue.length) job = jobQueue.shift();
  if (job) {
    const step = job.steps[job.i];
    g.position.x = ease(g.position.x, step.x, 1.2);
    crane.carriage.position.y = ease(crane.carriage.position.y, step.y, 0.8);
    crane.fork.position.z = ease(crane.fork.position.z, step.fork, 0.5);

    const arrived = Math.abs(g.position.x - step.x) < 0.01 &&
                     Math.abs(crane.carriage.position.y - step.y) < 0.01 &&
                     Math.abs(crane.fork.position.z - step.fork) < 0.01;
    if (arrived) {
      if (job.waitUntil == null) {
        step.onArrive?.();
        job.waitUntil = performance.now() + (step.holdMs || 0);
      }
      if (performance.now() >= job.waitUntil) {
        job.i++;
        job.waitUntil = null;
        if (job.i >= job.steps.length) job = null;
      }
    }
  }

  // crates not currently riding the fork ease toward their reported slot
  for (const m of crates.values()) {
    if (m.userData.riding) continue;
    if (m.userData.pendingJob) {
      // safety net: if several store events collapsed into one crane
      // broadcast (e.g. the bulk demo-scenario fill), only the last one
      // gets an animated job -- everything else still needs to arrive.
      if (performance.now() - m.userData.pendingSince > 1200) {
        m.userData.pendingJob = false;
        m.userData.goal = m.userData.pendingGoal;
      } else {
        continue;
      }
    }
    const goal = m.userData.goal;
    if (!goal) continue;
    m.position.x = ease(m.position.x, goal.x, 2.0);
    m.position.z = ease(m.position.z, goal.z, 2.0);
    m.position.y = ease(m.position.y, goal.y, 1.6);
  }

  if (follow) {
    controls.target.lerp(
      new THREE.Vector3(g.position.x, crane.carriage.position.y, 0), 0.08);
  }
  controls.update();
  renderer.render(scene, camera);

  if (hudEl && lastState) {
    hudEl.innerHTML =
      "<b>Rack</b> " + GEO.faces + " faces x " + GEO.cols + " col x " +
      GEO.levels + " lvl = " + (GEO.faces * GEO.cols * GEO.levels) + " slots<br>" +
      "<b>Envelope</b> 6.0 x 6.0 x 6.0 m &nbsp; used " +
      GEO.rackX.toFixed(1) + " x " + GEO.rackZ.toFixed(1) + " m<br>" +
      "<b>Crane</b> X " + g.position.x.toFixed(2) + " m &nbsp; Z " +
      crane.carriage.position.y.toFixed(2) + " m &nbsp; fork " +
      (crane.fork.position.z * 1000).toFixed(0) + " mm<br>" +
      "<b>Profile</b> S-curve, 1.2 / 0.8 m/s — gentle handling (fragile cores)";
  }
}
