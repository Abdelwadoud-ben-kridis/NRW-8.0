# contracts.md — freeze this before anyone writes code

Owner: **P3 (dashboard / backend / site)**. Everyone codes against this file.
If you change it, announce it out loud and bump the version line.

    CONTRACT VERSION: 1.10

Changes from 1.9 (pre-judging audit fixes, 2026-09-13):

- **Quantity: the weight count is the quantity; vision is a lower bound**
  (§3, `algo/engine.py::assess_box`). 1.7–1.9 accepted `min(weight,
  vision)` whenever the weight residual failed the tight tolerance, but the
  simulated camera can only MISS cores (occlusion), never invent them — so
  ordinary occlusion became a systematic undercount (8.8 % of honest 37-core
  boxes stored as 36; only 43 % read HAUTE). Now, when a vision count is
  available: the residual is judged against the noise of a box that size,
  `2.5·sqrt((CORE_MASS_CV·unit)²·count + SCALE_NOISE_G²)` (never tighter
  than the zero-vision tolerance); inside that band with the camera 0–2
  cores short → HAUTE, quantity = weight count; otherwise within 2 cores →
  MOYENNE, quantity = max(weight, vision); a gap of 3+ or a vision/barcode
  reference mismatch → QUARANTINE, unchanged. Calls with no vision reading
  keep the original tight tolerance. 37 × NY-114: 99.6 % exact, 98.8 % HAUTE.
- **A re-weigh can't clear a vision mismatch** (§2 recount, `backend/
  warehouse.py::recount_box`). A `QUARANTINE` box whose arrival vision
  reading named a different reference than its barcode is refused on a
  weight-only recount (reason `"vision : ref. X detectee a la reception,
  code-barre Y annonce Z -- relecture vision requise"`); only a recount
  with a fresh vision reading that agrees can re-admit it. Finding: the
  dashboard's Re-weigh button (weight only) re-admitted 65 % of
  vision-caught mismatches at qty 37 as the declared reference. A recount
  without a fresh reading also keeps the arrival's `vision_ref`/
  `id_confidence` instead of nulling them.
- **One box → one `box_done`** (§1.3, `firmware/sketch.ino`, fw tag `"1.3"`).
  `publishBoxDone()` used to call `resetBox()` mid-arrival; the board
  re-tared on the remaining settle frames and published a second
  `box_done` for every box (five for an empty crate) — swallowed by the
  backend dedup but logged as `box_done_ignored` each time. It now latches
  `DONE` (telemetry `state: "DONE"`) and ignores raw frames until the next
  `start_box`/`reset`. `STABLE_MS` 1200 → 2500 so the fallback timeout can
  no longer fire inside the plant model's 1.4 s settle window, before
  `final` arrives. The LED is switched off again. `tools/fake_device.py`
  (tag `"fake-1.3"`) is a line-for-line mirror again — it had drifted,
  which hid this from `tools/l0_probe.py`.
- **Unknown reference on `POST /api/demand` → 400** (§2). It used to open an
  `IN_PRODUCTION` batch for a part that doesn't exist. `IMPOSSIBLE` is now
  only "nothing free to allocate".
- **Cancelling a production batch keeps `payload.status` in sync** with the
  `status` column (§5 already promised this). New consistency check **O6**
  enforces it for every order (§7: 27 checks).
- **`POST /api/scenario` leaves the clock at ×1** (`config.SCENARIO_SPEED`),
  not ×60 — at ×60 the rehearsed numbers drifted within minutes and a
  2 sim-h reservation lock expired after 2 real minutes. **J** moves time.
- **`POST /api/sim/arrival` / `POST /api/sim/box`**: a non-integer `qty` or
  one outside `[0, 500]`, or an unknown `anomaly`, is a 400 instead of a 500.
- **Dashboard**: the demand form's "only N ready" hint is a warning, not a
  block, so a refusal with its ETA and a production batch can be triggered
  with **D**.
- **`SESSION` is `nrw8-scw-k7q2`** in `backend/config.py`,
  `firmware/sketch.ino` and `run.sh` (was the shared `nrw8`).
- Startup banner prints the port uvicorn was actually started on.

Changes from 1.8 (whole-box-only allocation again, and a reference-free
"take the oldest ready box" convenience, 2026-09-13):

- **FIFO picks are WHOLE-BOX-ONLY again, reversing 1.7's partial picks.**
  `algo/engine.py::fifo_allocate` now always takes a candidate box's
  entire `qty_available` — never a partial amount — so `qty_allocated`
  can overshoot `qty_requested` up to the next whole box instead of
  landing on it exactly. `picks[].partial` is always `false`; confirming
  an order always empties every box it reserved (`apply_pick`'s partial
  branch is unchanged but no longer reachable from `fifo_allocate`, same
  as it was before 1.7). Nothing else about contract 1.7 changed — vision
  cross-check, batch stock protection, and quarantine recovery are all
  still exactly as they were.
- **New `POST /api/demand/oldest`**: a convenience that skips picking a
  reference entirely — it reserves whichever `READY`, non-batch box (any
  reference) has been sitting longest, whole, by calling `W.reserve()`
  with that box's own `ref`/`qty_available`. Same audited FIFO path as an
  ordinary demand, not a separate code path; `{"error":"no ready boxes"}`
  (404) when nothing qualifies.
- Every place that documented or tested partial-pick behavior (`docs/
  contracts.md` itself, `docs/database-guide.md`, `README.md`,
  `algo/test_engine.py`, `tools/test_backend.py`, `tools/smoke.py`) was
  updated to whole-box-only expectations.
- `tools/smoke.py`, `tools/mqtt_probe.py`, and `tools/l0_probe.py` now
  read their target server's base URL from `SCW_BASE` (default unchanged:
  `http://localhost:8000`) instead of a hardcoded constant — they call
  `/api/reset` repeatedly and previously had no way to point away from
  whatever happened to be running on :8000.

Changes from 1.7 (remove the DHT22 / curing-room-climate feature entirely,
2026-09-13):

- **The DHT22 temperature/humidity sensor is gone from the firmware.**
  `firmware/sketch.ino` no longer includes the DHT library, reads GPIO 15,
  or carries `t_c`/`rh` in `box_done`/telemetry; `firmware/diagram.json`
  drops the `wokwi-dht22` part and its wiring; `firmware/libraries.txt`
  drops `DHT sensor library` and `Adafruit Unified Sensor`. Firmware tag
  bumped to `"1.2"` (`tools/fake_device.py` mirrors all of this, tag
  `"fake-1.2"`).
- **Why:** the sensor only ever fed an adaptive cure-time model that was
  dropped in contract 1.2 because the CDC never asked for one. Once that
  model was gone, the reading had no consumer at all -- not the cure
  calculation, not even the firmware's own commands (`onMessage`'s `T_CMD`
  handling never had an `"env"` case, so the backend's old
  `POST /api/sim/env` push to the board was already inert before this
  change). Keeping a sensor whose only job was to be displayed and ignored
  was flagged as exactly the kind of decorative complexity this project
  otherwise avoids.
- **The whole "curing room climate" concept is removed, backend to
  dashboard**, not just the physical sensor, since the same reasoning
  (nothing reads it) applied to the rest of the feature: `POST
  /api/sim/env` is deleted, `STATE["env"]` is gone from `backend/main.py`,
  `t_c`/`rh` are no longer accepted by `create_box`/`produce_for_batch`/
  `store_box`/`handle_box_done`, no longer attached to raw MQTT frames or
  arrival evidence, and no longer part of the `GET /api/state` snapshot.
  The dashboard's climate sliders and readouts are removed from
  `dashboard/index.html`/`app.js`/`labels.js`; the "Cure requirement
  (fixed)" readout (still exactly 24.0 h, always) is kept on its own.
- **Nothing about the cure rule itself changed.** It was already a fixed
  24 h for every box regardless of climate (contract 1.2) -- this contract
  just removes the now-pointless instrumentation around that fact, it does
  not touch the fact itself. `tools/test_firmware_contract.py` gained
  explicit "is gone" checks (mirroring how contract 1.7 checks the removed
  beam sensor); `tools/smoke.py`/`mqtt_probe.py`/`l0_probe.py` no longer
  call the deleted endpoint.

Changes from 1.6 (simulated vision cross-check, partial FIFO picks, batch
stock protection, and a way out of quarantine, 2026-09-13):

- **A simulated vision station now backs up the barcode** (criterion 2,
  "voir, identifier"). `backend/plant.py::simulate_vision` produces one
  shape+count reading per arrival (`len_mm`/`wid_mm`/`h_mm`/`holes`, plus
  `count_visible`) alongside the load-cell script, and `algo/engine.py::
  identify_core` matches it against the catalogue's own shape signatures
  (`articles.len_mm/wid_mm/h_mm/holes`, new columns). `backend/warehouse.py::
  create_box` passes the result into `assess_box`, which now:
    - **quarantines immediately if vision names a DIFFERENT reference** than
      the barcode declares, regardless of what the weight math says. This
      closes a real gap: for specific quantities, a wrong-reference swap
      could coincidentally land on a clean multiple of the wrong reference's
      mass and pass a weight-only check (finding: 1 case in 4 for the
      `mismatch` anomaly at certain quantities). `backend/plant.py::
      pick_swap_article` picks which real OTHER reference physically ends
      up in a `mismatch` crate, deterministically, and both the load-cell
      script and the vision reading agree on it.
    - **uses the vision core count as a second, independent measurement**
      of quantity. A weight reading that fails the tight zero-vision
      tolerance is no longer an automatic quarantine if vision confirms a
      close count (within 1-2 cores) -- it is accepted at MOYENNE using the
      more conservative figure. This is the fix for a real false-quarantine
      problem: at realistic ~3% per-core mass variance, the old
      weight-only tolerance wrongly quarantined 25-60% of honest boxes at
      some quantities; two independent sensors agreeing rescues them, and a
      gap of 3+ cores between the two sensors is still a hard quarantine
      (a disagreement that big is not plausible sensor noise).
  New box columns: `vision_ref`, `id_confidence`. `boxes.count_beam` is
  gone from every code path that matters (still 0 in the schema for
  compatibility) -- weight is the ESP32's only sensor; the second count
  comes from vision, not a beam.
- **FIFO picks are PARTIAL again, reversing contract 1.3.**
  `algo/engine.py::fifo_allocate` now takes only what a demand needs from
  each box in FIFO order -- `qty_allocated` lands exactly on
  `qty_requested` whenever the pipeline can cover it, instead of rounding
  up to the next whole box. The remainder of a partially-picked box stays
  `READY` at its ORIGINAL `t_in_sim` (this was already how `apply_pick`
  behaved; `fifo_allocate` simply generates a partial `take` again). A
  pick's shape gains `"partial": bool`.
- **A box tagged to a production batch (`boxes.batch_id`) is no longer
  general stock.** `fifo_allocate` excludes it entirely (new rejection
  reason `"reserve au lot"`), and `backend/warehouse.py::reserve`'s pipeline
  check for opening a NEW batch also excludes it. Finding fixed: a batch's
  own boxes used to be reachable by an unrelated demand, and the batch then
  shipped short with no warning. New consistency check `O5` proves this
  mechanically (no `batch_id` box is ever `RESERVED`).
- **An `IMPOSSIBLE` allocation now says WHEN, not just NO.** The result
  gains `"eta_sim"`: the simulated time the curing pipeline alone would
  close the gap (walking `DRYING` boxes oldest-ready-first), or `null` if
  even the whole pipeline can't (exactly the case where `reserve()` opens a
  production batch instead).
- **A ready batch ships itself.** `backend/warehouse.py::
  auto_ship_ready_batches`, called from `loop_clock`, ships every
  `IN_PRODUCTION` order that has nothing left `DRYING` without waiting for
  a manual confirm -- matching what this document already claimed a batch
  does. The manual ship button still works (confirming an already-shipped
  order is the existing idempotent no-op).
- **Quarantine is no longer a dead end.** Two new endpoints:
  `POST /api/box/{id}/archive` (`QUARANTINE`/`EMPTY` -> `ARCHIVED`, closing
  it out for good) and `POST /api/box/{id}/recount` (re-presents a
  `QUARANTINE` box's evidence -- a re-weigh, optionally a fresh vision
  reading -- through the same identification+quantity decision `create_box`
  would have made; on success the box re-enters `DRYING` from
  `t_in_sim = now`, same box, no second row). Only possible while the
  box's own barcode is still known with a real reference. New events:
  `box_archived`, `box_recount`. `algo/engine.py::_TRANSITIONS` gains
  `QUARANTINE -> DRYING`.
- **The raw MQTT frame carries an optional `"final": true` on its last
  frame** (`scw/<S>/sim/raw`, §1.1) -- a limit-switch-style signal that the
  crate has physically left the counting station, not a measurement. This
  closes a real race: at the original 1.2 s stability timeout against a
  ~1.4 s settle window, ordinary MQTT jitter could end a box early and
  clip the last core, and an undercount could still look like a clean
  whole number. `firmware/sketch.ino` (and `tools/fake_device.py`) shorten
  their stability wait once `final` has arrived instead of depending
  purely on the timeout, which stays only as a fallback.

Changes from 1.5 (make-to-order batches; capacity and overflow storage
removed, 2026-09-13):

- **`box_capacity` is gone entirely** — articles, `POST /api/articles`, and
  `algo/engine.py::assess_box` no longer have or check it. Nobody knows how
  many cores are in a box ahead of time; that is the entire reason the
  scale exists. A crate of any size is accepted as long as its weight is a
  clean multiple of its barcode's registered per-noyau mass.
- **Overflow storage is gone entirely** — `slots.zone`, the 12-slot
  STORAGE pool, and `POST /api/box/{id}/relocate` from contract 1.4 are all
  removed. The curing rack (`config.SLOT_COUNT`, 306 slots) is the only
  slot pool again.
- **Production demand is FIFO-first, make-to-order for the shortfall.**
  `POST /api/demand` still tries existing stock FIRST — FIFO, oldest whole
  box, exactly as before (criteria 5/6 are graded on choosing among
  EXISTING boxes, and that path is untouched). Only when the ENTIRE
  pipeline for that reference (including boxes still curing or held by
  another order, not just currently-free ones) genuinely can't cover the
  request does the order become a **production batch** (`status
  "IN_PRODUCTION"`) instead of `"IMPOSSIBLE"`. If there's already enough
  in the pipeline, just not free yet, nothing changes — the familiar
  `"sechage insuffisant"` / `"reserve"` refusal, unaffected.
- **A batch's boxes are produced, not picked** — `POST /api/sim/arrival`
  and `POST /api/sim/box` take a new optional `batch_order_id`; the
  resulting box is tagged to that order (`boxes.batch_id`) via the same
  scan/weigh path as any other arrival (no shortcut around identification
  just because the box was expected).
- **A batch ships as one unit, once every box tagged to it has left
  DRYING.** `POST /api/demand/confirm` on an `IN_PRODUCTION` order ships
  every `READY` box tagged to it (refusing with a 409 naming which boxes
  are still curing, until none are); a box lost to quarantine along the
  way still lets the rest ship, short and flagged (`"short": true`),
  rather than blocking forever on a replacement nobody asked for.
  `POST /api/demand/cancel` on a batch releases its boxes back to general
  stock (`batch_id` cleared) instead of discarding them.
- New order status: `IN_PRODUCTION`. New WebSocket snapshot field:
  `batches_pending` (live progress per open batch, always recomputed from
  `boxes.batch_id`, never cached).
- New event kinds: `batch_opened`, `batch_shipped`, `batch_cancelled`.
- `boxes.batch_id` column added (nullable, FK-in-spirit to
  `orders.order_id`).

Changes from 1.4 (barcode-first identification + overflow storage,
2026-09-13 — storage and capacity described here were REMOVED again in 1.6,
see above; identification stays as described):

- **Identification moved off the board, onto a barcode scan.** The real
  physical process: a worker labels a physical box and registers its own
  per-noyau weight (`POST /api/barcodes`) well before it ever reaches the
  conveyor. The conveyor's scanner then reads that barcode back — a lookup,
  not a guess — and only THEN does the box reach the scale. `algo/engine.py
  ::assess_box` no longer takes a beam count; quantity is purely
  `round(net_weight / that barcode's own unit_mass_g)`, and a large residual
  between the measured weight and any clean multiple of that value is what
  quarantines a box now, instead of a beam/weight cross-check disagreement.
  `boxes.count_beam` still exists in the schema and the API for
  compatibility but is always 0 and carries no meaning any more.
- **A barcode is one physical box, not a shared type.** Many barcodes can
  share one `ref` (article), each with its OWN measured `unit_mass_g` —
  batches vary slightly even within a reference. A barcode is consumed
  (`used_by_box` set) the instant it's scanned, accepted or quarantined; a
  second scan of the same barcode is quarantined as `"code-barre deja
  utilise"`, not treated as a second physical box.
- **New table `barcodes`** (`barcode_id` PK, `ref`, `unit_mass_g`,
  `registered_sim`, `used_by_box`) and **new `boxes.code`** column (the
  scanned `barcode_id`; NULL is impossible in practice since even an
  unknown scan still gets a QUARANTINE row naming it).
- **New anomaly set**: `none` / `mismatch` (the physical cores don't match
  what the barcode promised) / `empty` (no mass added at all). The old
  beam-cross-check anomalies (`off_by_one`, `delta`, `mislabel`,
  `sensor_dead`) no longer exist — there is no second sensor left to
  disagree with the scale.
- **New endpoints**: `POST /api/barcodes` (register), `GET /api/barcodes`
  (list, `?unused=true` filters to not-yet-scanned). `POST /api/sim/arrival`
  and `POST /api/sim/box` now take `barcode_id` (the real workflow) OR the
  convenience `ref` (auto-registers a throwaway barcode on the spot, so a
  quick demo/test press still needs zero setup).
- **The wire `box_done`/`start_box` field is still literally named `ref`**
  for firmware-compatibility — the board only ever echoes it back, never
  parses it, so nothing on the ESP32 changed when identification moved to a
  barcode scan. Its content is a `barcode_id`, not an article reference.
- **A cured box can move to overflow storage** instead of waiting in the
  curing rack for a pickup order: `POST /api/box/{id}/relocate` moves a
  `READY`, unlocked box from the 306-slot curing rack (`slots.zone
  ='CURING'`) into a separate 12-slot flat pool (`zone='STORAGE'`,
  `config.STORAGE_SLOTS`), freeing its curing slot for a new arrival without
  touching its cure record or FIFO position (`t_in_sim` unchanged, so it
  stays exactly as pickable as before).
- New WebSocket snapshot fields (§3): `boxes[].code`, `boxes[].zone`,
  `kpi.storage_total`/`storage_used`/`storage_free`.
- New event kinds: `barcode_registered`, `box_relocated`.

Changes from 1.3 (box_capacity enforcement, 2026-09-13):

- **A crate cannot hold more cores than its declared `box_capacity`**
  (§1.3, `algo/engine.py::assess_box`). An otherwise-accepted count above
  capacity is now quarantined (reason `"quantite N superieure a la
  capacite de la caisse (CAP)"`) instead of silently stored as one
  oversized box. This runs after identification/quantity, so a
  wrong-reference or counting-delta fault is still reported as that fault.
- **`POST /api/sim/arrival` and `POST /api/sim/box` split an over-capacity
  request into several right-sized crates** instead of ever reaching the
  new capacity quarantine themselves (`backend/main.py::split_for_capacity`):
  asking for 70 cores of a 69-capacity reference creates two boxes (69 + 1),
  sequentially, the same as if two crates had physically arrived one after
  the other. A real device's `box_done` is one physical crate and is never
  split — if its own count exceeds capacity, that box is quarantined.

Changes from 1.2 (whole-box allocation, 2026-09-13):

- **FIFO allocation never splits a box** (§4, §6.7). A pick always takes a
  candidate box's entire `qty_available`; there is no more partial `take`
  out of `fifo_allocate`. Covering an order can therefore require rounding
  up to the next whole box, so **`qty_allocated` may exceed
  `qty_requested`** — that is expected, not a bug (`backend/consistency.py`
  §O1 no longer flags it).
- **Not enough whole-box stock refuses the whole reservation** (§4, §6.7).
  If the total `qty_available` across every pickable box for `ref` is less
  than `qty_requested`, `POST /api/demand` reserves nothing at all
  (`status: "IMPOSSIBLE"`, `picks: []`) instead of handing out whatever was
  available and reporting a shortfall. Every otherwise-pickable box still
  appears in `rejected[]` with the new reason `"stock insuffisant"`, so the
  jury sees a stock problem, not a state problem.
- `apply_pick` (used by confirm) is unchanged and still technically supports
  a partial take — but since `fifo_allocate` never generates one anymore,
  confirming an order always empties every box it reserved.

Changes from 1.1 (database/backend hardening pass, 2026-09-12):

- **Fixed 24 h cure, no adaptive model** (§6.2). The CDC never asked for a
  climate-adaptive cure time; it asks for the 24 h threshold, full stop.
  `t_c`/`rh` remain on every box as arrival evidence and on the HMI as a
  monitoring readout, but nothing computes with them anymore.
- **Idempotent confirm/cancel, and a real refusal** (§2, §6.5). Confirming
  an already-DONE order is a no-op; confirming a CANCELLED/IMPOSSIBLE order
  is a 409, not a silent double-deduction. Cancelling an already-CANCELLED
  order is a no-op; cancelling a DONE/IMPOSSIBLE order is a 409.
- **Reservation expiry cancels the ORDER**, not just the box (§6.5). A
  lock that runs out turns its order CANCELLED (an `order_expired` event
  fires) and releases every box it was still holding — it no longer leaves
  a PENDING order pointing at boxes that quietly went back to READY.
- **`slots.reserved_for` and `boxes.locked_by` are actually maintained**
  now (§5) — set on reserve, cleared on confirm/cancel/expiry, in the same
  transaction as the box they describe.
- **Unknown reference → `article_ref = NULL`**, not a fallback article
  (§1.3, §5). A quarantined box for an unrecognised `ref` no longer
  pollutes a real reference's stock panel or FIFO rejected list.
- **FIFO's box_id tiebreak is numeric**, not lexicographic (§6.3) — `BOX-2`
  sorts before `BOX-10`.
- **Duplicate `box_done` is handled** (§1.3) — a late device answer after
  an L1 fallback, or two devices on one session, no longer creates a
  second box for the same physical arrival. No new payload fields.
- **`POST /api/reset` honours `{"seed": false}`** (§2) — previously
  accepted and silently ignored.
- `POST /api/replay` **is not implemented.** L2 in practice is: restart the
  process, press **S**. Removed from the endpoint table below.
- The `rejected[]` reason/detail strings in §4 are given **exactly as the
  engine emits them** (unaccented ASCII) — the dashboard translates them
  for display (`dashboard/app.js::tRej/tDet`); do not expect accents in
  the raw API response.
- Undocumented-but-real endpoints added to §2: `GET /api/slots`,
  `GET /api/anomalies`, `POST /api/sim/arrival`, `POST /api/scenario`,
  `GET /api/db/check`, `GET /api/db/box/{id}`.
- New WebSocket snapshot fields (§3): `contract_version`, `orders_pending`.

---

## 0. Topology

```
 [ backend/plant.py ]  = PLANT MODEL (the physical world, server-side)
        |               raw load-cell mV + one simulated vision reading
        v
 [ FastAPI backend ] --MQTT--> scw/sim/raw  ------> [ ESP32 (Wokwi or real) ]
        ^                                             | tare, stability, report
        |                                             | settled mass (once per box)
        +---MQTT--- scw/dev/telemetry ----------------+
        +---MQTT--- scw/dev/box_done  ----------------+
        |
   [ SQLite ] <-- backend/warehouse.py (transactions) <-- algo/engine.py (pure, no I/O)
        |
        +--WS--> dashboard (HMI + 3D twin) 5 Hz state push
```

The plant model (`backend/plant.py`) runs in the backend process, not in the
browser — the 3D twin (`dashboard/twin.js`) is a pure WS **consumer**, it
never generates or forwards a sensor signal. `POST /api/sim/arrival` (the
**A** hotkey) plays one arrival's frames over MQTT at 10 Hz; the WS
client→server `{"type":"raw",...}` message in §3 exists as a manual
fast path (used by `POST /api/sim/raw`) but the dashboard's own JS never
sends it.

The ESP32 is **never told the answer**. It receives the raw load-cell signal only
and derives the tared, settled mass itself; identity (barcode + simulated vision)
and the count are decided one layer up, in `algo/engine.py`. That is what makes
criterion 9 (embedded, 15 pts) real instead of decorative.

MQTT broker: `broker.hivemq.com:1883` (TCP for Python, 1883 for Wokwi's
`WiFi.begin("Wokwi-GUEST","")`). Every topic is prefixed with a **session id** so
two laptops in the same room do not collide:

    scw/<SESSION>/sim/raw
    scw/<SESSION>/dev/telemetry
    scw/<SESSION>/dev/box_done
    scw/<SESSION>/dev/cmd

`SESSION` is set in `backend/config.py` and in `firmware/sketch.ino`. **They must
match.** Current value: `nrw8-scw-k7q2` (contract 1.10 — never the shared `nrw8`);
`tools/test_firmware_contract.py` fails if the two files disagree.

---

## 1. MQTT payloads

### 1.1 `scw/<S>/sim/raw` — backend → ESP32, 10 Hz

```json
{ "beam": 1, "load_mv": 1843, "t_sim": 93600.0 }
{ "load_mv": 1843, "t_sim": 93601.0, "final": true }
```

| field     | type  | meaning                                                        |
|-----------|-------|----------------------------------------------------------------|
| `beam`    | 0/1   | legacy/unused (contract 1.7 -- weight is the only sensor); still accepted if present, never read |
| `load_mv` | int   | load-cell amplifier output, 0..3300 mV (0 mV = 0 g, 3300 = 30 kg)|
| `t_sim`   | float | simulated clock, seconds. **Never wall clock.**                 |
| `final`   | bool, optional | contract 1.7: set only on the LAST frame of an arrival -- a limit-switch-style "the crate has left the counting station" signal, not a measurement. Lets the firmware shorten its stability wait instead of relying purely on a timeout that ordinary MQTT jitter could clip a core off of. Omitted/false on every other frame. |

Mass conversion used by BOTH sides (hard-coded constant, do not change after H2):

    grams = load_mv * (30000.0 / 3300.0)      # 9.0909 g per mV

### 1.2 `scw/<S>/dev/telemetry` — ESP32 → backend, 2 Hz

```json
{ "state":"COUNTING", "gross_g":9420.5,
  "stable":false, "up_ms":128400, "src":"esp32" }
```

`state` ∈ `IDLE | COUNTING | STABILIZING | DONE | FAULT` — `DONE` (contract
1.10) from the moment the box is reported until the next `start_box`/`reset`.
This is telemetry only — it drives the "live ESP32" panel on the dashboard.
It never mutates the database.

### 1.3 `scw/<S>/dev/box_done` — ESP32 → backend, once per box

**This is the only message that creates a box.**

```json
{ "ref":"BC-1042", "gross_g":9420.5, "fw":"1.3" }
```

`ref` carries the scanned `barcode_id` (field name kept for firmware
compatibility, contract 1.5). Exactly one `box_done` per physical box
(contract 1.10), normally on the arrival's `final` frame.

Backend response: run `algo.engine.assess_box(...)`, INSERT into `boxes`,
assign a slot, broadcast over WS. If `ref` is unknown → box is created in
`QUARANTINE` with `article_ref = NULL` and reason `reference inconnue: <ref>`
(contract 1.2 — no longer misfiled under a fallback article).

**Duplicate delivery (contract 1.2).** The backend tracks one "arrival
window" for the box currently expected on the conveyor (opened by
`POST /api/sim/arrival`, closed by the first accepted `box_done` or by the
L1 fallback). A `box_done` is deduplicated using only the fields already in
the payload above — no new field is added:

| situation | outcome |
|---|---|
| first `box_done` while a window is open for the same `ref` | creates the box, closes the window |
| a second `box_done` for an already-closed window, within ~15 s (a late ESP32 answer after L1 already fired) | ignored, logged as `box_done_ignored` |
| a `box_done` with no open window, identical to the last such message within ~5 s (two devices on one session, or a repeated publish) | ignored, logged as `box_done_ignored` |
| anything else (no window, or different evidence) | creates a new box |

A malformed payload (wrong types, missing `ref`, out-of-range values) is
rejected before it reaches `assess_box` and logged as `box_done_invalid` —
it never crashes the MQTT listener, and telemetry/curing keep running.

### 1.4 `scw/<S>/dev/cmd` — backend → ESP32

```json
{ "cmd":"tare" }
{ "cmd":"start_box", "ref":"NY-114" }
{ "cmd":"reset" }
```

---

## 2. REST API (FastAPI, same origin as the dashboard — no CORS)

| method | path                  | body                                   | returns |
|--------|-----------------------|----------------------------------------|---------|
| GET    | `/api/state`          | –                                      | full snapshot (see §3) |
| GET    | `/api/articles`       | –                                      | `[article]` |
| POST   | `/api/articles`       | `{"ref":"NY-450","label":"...","unit_mass_g":310.0}` | new article row, or `{"error":...}` (400) |
| GET    | `/api/slots`          | –                                      | `[slot]`, the raw rack table |
| GET    | `/api/anomalies`      | –                                      | `{key: label}` — options for the plant-model anomaly picker |
| POST   | `/api/clock`          | `{"speed":60}` or `{"jump_h":6}`       | `{t_sim, speed}`, or `{"error":...}` (400: `speed` must be one of `config.ALLOWED_SPEEDS`, `jump_h` must be in `(0, config.MAX_JUMP_H]`) |
| POST   | `/api/barcodes`       | `{"barcode_id":"BC-1042","ref":"NY-114","unit_mass_g":206.0}` | a worker's registration, ahead of any arrival — new row, or `{"error":...}` (400 bad/unknown ref or mass; 409 duplicate barcode_id) |
| GET    | `/api/barcodes?unused=true` | –                                 | `[{"barcode_id","ref","unit_mass_g","registered_sim","used_by_box"}]` |
| POST   | `/api/sim/arrival`    | `{"barcode_id":"BC-1042","qty":37,"anomaly":"none"}` or `{"ref":"NY-114",...}` (auto-registers a throwaway barcode) — optional `"batch_order_id":"ORD-3"` tags the resulting box to that production batch | plays the plant model at 10 Hz, then L0/L1 as in §0 — this is the **A** hotkey. 400 if `qty` is not an integer in `[0, 500]` or `anomaly` is not a key of `GET /api/anomalies` (contract 1.10) |
| POST   | `/api/demand`         | `{"ref":"NY-114","qty":40}`            | allocation plan (§4) — `status` is `"PENDING"` (FIFO fully covered it), `"IN_PRODUCTION"` (opened a batch for the shortfall), or `"IMPOSSIBLE"` (nothing free to allocate); or `{"error":...}` (400: missing, invalid or unknown `ref` — contract 1.10 — or `qty` not a positive integer) |
| POST   | `/api/demand/oldest`  | – (no body)                            | contract 1.9: skip picking a reference — reserves whichever `READY`, non-batch box (any reference) has the oldest `t_in_sim`, whole. Same allocation plan shape as `/api/demand`, `qty_requested` set to that box's own `qty_available` so it never overshoots itself; or `{"error":"no ready boxes"}` (404) |
| POST   | `/api/demand/confirm` | `{"order_id":"ORD-3"}`                 | PENDING: `{"ok":true,"already":false,"qty_allocated":N}`. IN_PRODUCTION: ships every READY box tagged to the batch — `{"ok":true,"already":false,"qty_allocated":N,"target":M,"short":bool}`, or a 409 naming which boxes are still curing. Confirming an already-DONE order returns `{"ok":true,"already":true}`; confirming a CANCELLED/IMPOSSIBLE/unknown order is `{"error":...}` (409/404) |
| POST   | `/api/demand/cancel`  | `{"order_id":"ORD-3"}`                 | PENDING: releases its picks back to READY. IN_PRODUCTION: clears `batch_id` on its boxes, returning them to general stock. Both: `{"ok":true,"already":false,"released":[box_id,...]}` — cancelling an already-CANCELLED order returns `{"ok":true,"already":true}`; cancelling a DONE/IMPOSSIBLE/unknown order is `{"error":...}` (409/404) |
| POST   | `/api/sim/raw`        | `{"beam":0,"load_mv":1843}`            | `{ok:true}` — plant model → MQTT |
| POST   | `/api/sim/box`        | `{"barcode_id":"BC-1042","qty":37}` or `{"ref":"NY-114",...}`, optional `"batch_order_id"` | L1 FALLBACK: create a box without the ESP32 |
| POST   | `/api/reset`          | `{"seed":true}` (default) or `{"seed":false}` | wipe + reseed everything, or (with `seed:false`) wipe boxes/orders/events/slots/barcodes but keep the current `articles` |
| POST   | `/api/scenario`       | `{"name":"demo"}`                      | loads the rehearsed 6-box / 34 h demo history (full reseed) — the **S** hotkey. Leaves the clock at ×1 (`config.SCENARIO_SPEED`, contract 1.10) |
| GET    | `/api/events?limit=200` | –                                    | event log |
| GET    | `/api/db/check`       | –                                      | read-only consistency report (§7 below) |
| GET    | `/api/db/box/{id}`    | –                                      | one box's row + slot + every event/order that names it |
| POST   | `/api/box/{id}/archive` | –                                     | contract 1.7: `QUARANTINE`/`EMPTY` -> `ARCHIVED`, closing the box out for good -- `{"ok":true,"box_id":...,"state":"ARCHIVED"}`, or `{"error":...}` (404 unknown box, 409 wrong state) |
| POST   | `/api/box/{id}/recount` | `{"gross_g":9420.5}`, optional `{"anomaly":"none"}` to also take a fresh simulated vision reading | contract 1.7: re-presents a `QUARANTINE` box's evidence through the same identification+quantity decision as a fresh arrival. `{"ok":true,"accepted":bool,...}` -- on success the box re-enters `DRYING` from `t_in_sim = now`; on failure it stays `QUARANTINE` with updated evidence. A box whose arrival vision reading named a different reference than its barcode is refused (`accepted: false`) unless this recount includes a fresh vision reading (contract 1.10). `{"error":...}` (404 unknown box; 409 wrong state, or no known/valid barcode to recount against) |

`POST /api/articles` — `ref`, `label`, `unit_mass_g` are required; `tolerance_g`
(default ~3 % of `unit_mass_g`) and `color` (default: next unused colour from
a fixed palette) are optional. There is no `box_capacity` (contract 1.6 —
nobody knows how many cores are in a box ahead of time; that's what the
scale is for). A `cure_floor_h` in the body is accepted but **ignored** —
contract 1.2 fixes drying at 24 h for every reference, so there is no
per-reference exception to request. A duplicate `ref` or a non-positive
`unit_mass_g` is a 400. This is how `dashboard/index.html`'s "+ New
reference" form (in the ⋯ menu) adds a type without a restart — it is
additive only, so `backend/db.py::ARTICLES` still owns the four references
the rehearsed demo depends on.

Static: `GET /` serves `dashboard/index.html`. Everything under `/static/*` is
the `dashboard/` folder.

---

## 3. WebSocket `/ws`

Server → client, one JSON object per frame, ~5 Hz:

```json
{
  "type": "state",
  "contract_version": "1.10",
  "t_sim": 93600.0,
  "speed": 1,
  "clock_label": "J+1 02:00",
  "device": {"online":true,"state":"COUNTING","count_beam":0,
             "gross_g":9420.5,"last_seen_sim":93598.0},
  "kpi": {"slots_total":306,"slots_used":37,"boxes_ready":12,
          "boxes_drying":9,"boxes_quarantine":1,"cores_available":431},
  "boxes": [ /* see below */ ],
  "orders_pending": [ {"order_id":"ORD-3","ref":"NY-114","qty_requested":40,
                       "qty_allocated":40,"lock_expires_sim":97200.0,
                       "lock_remaining_s":1800.0} ],
  "batches_pending": [ {"order_id":"ORD-5","ref":"NY-114","target":30,
                        "boxes":[{"box_id":"BOX-9","state":"DRYING","qty":30}],
                        "produced":0,"still_drying":["BOX-9"],
                        "lost_to_quarantine":[],"ready_to_ship":false} ],
  "slots_occupancy": {"F0-C3-L7":"BOX-12", ...},
  "last_order": { /* §4 */ },
  "crane": {"cmd":"store","box_id":"BOX-12","slot_id":"F0-C3-L7"}
}
```

`orders_pending` lists every `PENDING` order (not only `last_order`), with
its lock countdown — added in contract 1.2 so the HMI can show a
reservation about to expire before it happens (§6.5).

`batches_pending` (contract 1.6) lists every `IN_PRODUCTION` order — a
demand FIFO couldn't fully cover, now being made to order (§4, §6.7). Every
field is recomputed live from `boxes.batch_id` on each snapshot, never
cached in the order's own payload, so it can never drift from what has
actually cured. `ready_to_ship` is true once no tagged box is still
`DRYING` (a `QUARANTINE` box does not block shipping, it just doesn't
count toward `produced`).

Box object (this exact shape is what the 3D twin and the table both read):

```json
{ "box_id":"BOX-12", "ref":"NY-114", "label":"Noyau culasse 114",
  "code":"BC-1042", "batch_id":null,
  "qty_initial":37, "qty_available":37, "slot_id":"F0-C3-L7",
  "state":"DRYING", "t_in_sim":60000.0, "required_cure_h":24.0,
  "ready_at_sim":146400.0, "cure_pct":38.9,
  "count_beam":0, "count_weight":37, "gross_g":9420.5,
  "confidence":"HAUTE", "reason":null,
  "vision_ref":"NY-114", "id_confidence":"HAUTE",
  "locked_by":null, "lock_expires_sim":null }
```

`vision_ref`/`id_confidence` (contract 1.7) are the simulated vision
station's own read on this box's reference and how confident that match
was (`algo/engine.py::identify_core`) -- both `null` when no vision reading
was taken for this arrival (the manual "quick box" shortcut has no camera
either). `confidence` remains the QUANTITY confidence (weight, optionally
rescued or overruled by the vision core count); `id_confidence` is a
separate, IDENTIFICATION confidence -- the two answer different questions
and can disagree (e.g. `id_confidence` HAUTE with `confidence` MOYENNE: the
camera is sure this is the right part, but the two counts differ by one).

`code` is the `barcode_id` the conveyor's scanner read off this physical
crate (contract 1.5) — a worker registered it, with its own per-noyau
weight, before the box ever arrived (`POST /api/barcodes`). It is never
read back to make a decision; the residual weight check in `assess_box` is
still the only thing that can catch a mismatched box. `batch_id` (contract
1.6) is the order this box was produced for, when existing stock couldn't
cover a demand and a production batch opened for it — `null` for ordinary
stock. `count_beam` still exists in the schema/API for compatibility but is
always `0` and carries no meaning any more (contract 1.5 — the scale is the
only sensor).

`ref` is `null` (and `label` falls back to the quarantine `reason`) for a
box quarantined against an unrecognised reference (contract 1.2, §1.3) —
render that case instead of assuming `ref` is always a known article.

Client → server (rare; most client actions go through REST):

```json
{ "type":"raw", "beam":0, "load_mv":1843 }     // plant model fast path
{ "type":"ping" }
```

---

## 4. Allocation result (`POST /api/demand` and `last_order`)

```json
{
  "order_id": "ORD-3",
  "ref": "NY-114",
  "qty_requested": 35,
  "qty_allocated": 40,
  "shortfall": 0,
  "eta_sim": null,
  "picks": [
    {"box_id":"BOX-4","slot_id":"F0-C1-L2","take":22,"t_in_sim":3600.0,"rank":1,"partial":false},
    {"box_id":"BOX-9","slot_id":"F1-C5-L9","take":18,"t_in_sim":9000.0,"rank":2,"partial":false}
  ],
  "rejected": [
    {"box_id":"BOX-7","reason":"sechage insuffisant","detail":"pret dans 4.2 h"},
    {"box_id":"BOX-2","reason":"quarantaine","detail":"ecart de comptage : pesee 30, vision 27 noyaux"},
    {"box_id":"BOX-11","reason":"reserve","detail":"ORD-2"},
    {"box_id":"BOX-14","reason":"reserve au lot","detail":"ORD-5"},
    {"box_id":"BOX-15","reason":"plus recent (FIFO)","detail":"besoin deja couvert par des box plus anciennes"}
  ],
  "status": "PENDING"
}
```

Picks are **whole-box-only** (contract 1.9, reversing 1.7's partial
picks): `take` is always a candidate box's entire `qty_available`, so
`qty_allocated` can OVERSHOOT `qty_requested` up to the next whole box
(35 requested, 40 allocated, above) rather than landing on it exactly —
`picks[].partial` is always `false`. A box already tagged to another
order's production batch (`"reserve au lot"`) is excluded from `pickable`
entirely — it is not general stock (§6 rule 8b).

When nothing is pickable (`status: "IMPOSSIBLE"`), `eta_sim` (contract 1.7)
names the simulated time the curing pipeline ALONE would close the gap
(walking `DRYING` boxes oldest-ready-first from whatever is already READY),
or `null` when even the whole pipeline can't — exactly the situation
`backend/warehouse.py::reserve` answers by opening a production batch
instead. `eta_sim` is `null` on every other status.

If `fifo_allocate` finds nothing pickable (`picks: []`), `backend/warehouse.py
::reserve` looks at the WHOLE pipeline for that ref next (§6 rule 8):

- **Enough exists somewhere, just not free yet** (curing or held by
  another order) — unchanged: `status` is `"IMPOSSIBLE"`, and every
  otherwise-pickable box appears in `rejected[]` with reason
  `"stock insuffisant"` (detail: `"N disponible(s) au total pour M
  demande(s)"`).
- **Genuinely not enough anywhere** — the order opens a production batch
  instead: `status` is `"IN_PRODUCTION"`, a `"target"` field is added
  (the full `qty_requested`), and `rejected[]` still lists why nothing
  existing was pickable. See `batches_pending` (§3) for how the batch's
  live progress is reported afterward.

`reason`/`detail` are given here exactly as `algo/engine.py` emits them:
unaccented ASCII, since that is also what `algo/test_engine.py` asserts on.
`dashboard/app.js::tRej/tDet` translates them for display — do not expect
accented French straight from the API.

**The `rejected` list is not optional.** It is how the jury *sees* FIFO being
enforced instead of taking your word for it. Render it on screen, always.

---

## 5. Database (SQLite, WAL)

```sql
articles(ref PK, label, unit_mass_g REAL, tolerance_g REAL,
         cure_floor_h REAL DEFAULT 24.0,
         len_mm REAL, wid_mm REAL, h_mm REAL, holes INT, color TEXT)
  -- no box_capacity (contract 1.6) -- nobody knows how many cores are in a
  -- box ahead of time, that's what the scale is for. len_mm/wid_mm/h_mm/
  -- holes (contract 1.7) are the shape signature algo/engine.py::
  -- identify_core matches a simulated vision reading against.

boxes(box_id PK, article_ref FK NULL, qty_initial INT, qty_available INT,
      slot_id FK NULL, state TEXT, t_in_sim REAL, required_cure_h REAL,
      ready_at_sim REAL, count_beam INT, count_weight INT, gross_g REAL,
      confidence TEXT, reason TEXT, locked_by TEXT NULL,
      lock_expires_sim REAL NULL, code TEXT NULL, batch_id TEXT NULL,
      vision_ref TEXT NULL, id_confidence TEXT NULL)
  -- article_ref is NULL only while state='QUARANTINE' or 'ARCHIVED' (an
  -- unknown/reused-barcode quarantine, contract 1.5, that was later closed
  -- out via archive_box without ever being identified, contract 1.7). A
  -- partial unique index on slot_id (WHERE NOT NULL) makes "one box per
  -- slot" DB-enforced, not just convention.
  -- `code` is the barcode_id the conveyor's scanner read off this crate
  -- (contract 1.5) -- never read back to make a decision. `batch_id`
  -- (contract 1.6) is the IN_PRODUCTION order this box was produced for,
  -- when existing stock couldn't cover a demand; NULL for ordinary stock;
  -- such a box is invisible to every OTHER order's allocation (contract 1.7).
  -- `count_beam` is always 0, kept only for API/schema compatibility.
  -- `vision_ref`/`id_confidence` (contract 1.7) are the simulated vision
  -- station's own identification read on this box, independent of the
  -- barcode -- both NULL when no vision reading was taken.

slots(slot_id PK, face INT, col INT, level INT,
      occupied_by FK NULL, reserved_for FK NULL)
  -- occupied_by/reserved_for are soft references (see database-guide.md),
  -- but a partial unique index on occupied_by (WHERE NOT NULL) makes "one
  -- slot per box" DB-enforced. Both are maintained by backend/warehouse.py
  -- in the same transaction as the box they describe. No zone (contract
  -- 1.6 removed overflow storage) -- every slot is the curing rack.

barcodes(barcode_id PK, ref FK NOT NULL, unit_mass_g REAL, registered_sim REAL,
         used_by_box TEXT NULL)
  -- a worker's registration of ONE physical box (contract 1.5), well
  -- before it ever arrives -- not a shared type like articles. `used_by_box`
  -- is set the instant the conveyor's scanner reads this barcode back
  -- (backend/warehouse.py::create_box); a barcode is consumed exactly once.

orders(order_id PK, ref, qty_requested INT, qty_allocated INT,
       status TEXT, created_sim REAL, payload TEXT)
  -- payload.status is kept equal to the status column on every write.
  -- status is PENDING | IN_PRODUCTION | DONE | CANCELLED | IMPOSSIBLE
  -- (IN_PRODUCTION added in contract 1.6 -- see backend/warehouse.py::reserve).

events(id PK AUTOINCREMENT, t_sim REAL, kind TEXT, payload TEXT)
  -- inserted in the SAME transaction as the mutation it describes
  -- (backend/db.py::log_event no longer commits on its own).

meta(k PK, v)
  -- holds the simulated-clock checkpoint (k='t_sim', k='speed'), restored
  -- on backend startup so a restart does not rewind time (contract 1.2).
```

**Persisted states** — what `boxes.state` actually holds:
`DRYING → READY → RESERVED → (cancelled/expired → READY, t_in unchanged |
confirmed → EMPTY, always the whole box, contract 1.9) → ARCHIVED`, plus
`QUARANTINE` (a box is born directly into
`DRYING` or `QUARANTINE`). Contract 1.7 adds two ways out of `QUARANTINE`:
`POST /api/box/{id}/archive` (`QUARANTINE`/`EMPTY` → `ARCHIVED`, terminal)
and `POST /api/box/{id}/recount` (`QUARANTINE` → `DRYING` on a successful
re-weigh, `t_in_sim` reset to the recount time).

`INCOMING → IDENTIFYING → COUNTING → STORING` and `PICKING` are **virtual**
stages — real-world moments narrated in event payloads (`box_in`'s
evidence, `pick_done`'s per-box detail) but never written to `boxes.state`.
The whole reserve-then-pick sequence is one atomic backend operation, not
two, so `PICKING` is never observed mid-flight. See `algo/engine.py`'s
`PERSISTED_STATES`/`_TRANSITIONS` and `docs/database-guide.md`.

`orders.status`: `PENDING → DONE | CANCELLED`, or born straight into
`IMPOSSIBLE` (zero picks: enough exists in the pipeline but none of it is
free yet). An unknown ref never becomes an order — it is a 400 (contract 1.10). Confirming/cancelling an
order already in its target state is a no-op (`{"already": true}`);
confirming/cancelling from any other state is a 409. An expired reservation
also lands on `CANCELLED` (event kind `order_expired`, distinct from a
manual `order_cancel`).

`IN_PRODUCTION` (contract 1.6) is the other birth outcome: existing FIFO
stock across the WHOLE pipeline for that reference (curing or reserved
included, not just currently-free) can't cover the request, so a
production batch opens instead of `IMPOSSIBLE`. `IN_PRODUCTION → DONE` is
`POST /api/demand/confirm` shipping every `READY` box tagged to the batch
(`boxes.batch_id`) once none is left `DRYING` — see §6 rule 8 below.
`IN_PRODUCTION → CANCELLED` clears `batch_id` on its boxes, returning them
to general stock instead of discarding them.

---

## 6. Rules that are not negotiable

1. **No wall-clock timestamps anywhere.** Everything is `t_sim` in seconds.
   If you ever write `datetime.now()` into the DB, the 24 h demo breaks.
   (The clock checkpoint in `meta` and device-liveness checks legitimately
   use `time.monotonic()`, but only in backend runtime memory — never
   written to a warehouse column.)
2. `required_cure_h = 24.0`, always, for every box and every reference —
   **fixed, not adaptive** (contract 1.2). There is no climate sensor or
   input anywhere in this system any more (contract 1.8 removed the DHT22
   that used to feed the dropped adaptive model).
3. FIFO sort key is `(t_in_sim, box_id)`, and the `box_id` tiebreak is
   compared **numerically** (`algo.engine.fifo_key`), not as a string —
   `BOX-2` sorts before `BOX-10`. Every place that orders boxes for FIFO
   purposes (allocation, the inventory table, the by-ref FIFO head) uses
   this same key.
4. `apply_pick` (confirm) supports a partial take in principle and would
   return the box to `READY` with **`t_in_sim` unchanged** if it ever got
   one — re-stamping it would silently break FIFO. In practice this never
   fires, because rule 7 means `fifo_allocate` never generates a partial
   take.
5. `RESERVED` is a real lock with `lock_expires_sim`; expiry releases the
   box **and cancels its order** (`order_expired` event) — a lock running
   out never leaves a `PENDING` order pointing at boxes that already moved
   on.
6. Confirm and cancel are **idempotent, not re-appliable**: repeating either
   on an order already in its target state is a no-op; applying either to
   an order in the wrong state is refused (409), never silently applied to
   whatever the boxes happen to be now.
7. **A pick is WHOLE-BOX-ONLY** (contract 1.9, reversing 1.7's partial
   picks): `take` is always a candidate box's entire `qty_available` —
   never split — so `qty_allocated` may OVERSHOOT `qty_requested` up to
   the next whole box rather than landing on it exactly. `picks[].partial`
   is always `false`.
8. **Demand is FIFO-first, make-to-order for the shortfall** (contract
   1.6): if the ref's total pipeline (every non-`QUARANTINE`,
   non-`batch_id` box, curing or reserved included, not just
   currently-free) can't reach `qty_requested`, the order opens a
   production batch (`IN_PRODUCTION`) instead of being refused. If enough
   already exists somewhere in the pipeline — just not free yet — nothing
   changes: the order stays `IMPOSSIBLE`, `picks: []`, with the ordinary
   drying/reserved refusal reasons (and `eta_sim` naming when the pipeline
   alone would close the gap, contract 1.7).
8b. **A box tagged to a production batch is not general stock** (contract
   1.7): `fifo_allocate` excludes any box with `batch_id` set from
   `pickable` entirely (reason `"reserve au lot"`), and the pipeline check
   in rule 8 excludes it too — one demand can no longer reserve or count
   toward its own coverage a box another order is waiting on. A ready
   batch also ships itself once nothing it produced is still `DRYING`
   (`backend/warehouse.py::auto_ship_ready_batches`, called from
   `loop_clock`), without needing a manual confirm.
9. Every backend operation that touches more than one row runs inside one
   database transaction (`backend/db.py::transaction`, used throughout
   `backend/warehouse.py`) — a crash or a refused precondition leaves
   nothing half-written.
10. All UI strings come from `dashboard/labels.js`. Nobody hard-codes a
    string in the HTML. Language switch = one line.

---

## 7. Consistency checker

`GET /api/db/check` (`backend/consistency.py`) runs 27 read-only checks —
foreign-key-style integrity, box/order state shape (including O6,
`payload.status` == `status`, contract 1.10), lock consistency, cure
timing, article sanity — and returns `{"overall": "PASS"|"WARN"|"FAIL",
"t_sim", "checks": [{"id","severity","count","offending","message"}]}`.
It is exposed as a badge on the DB Explorer (`/db`). A demo, a chaos test,
or a rehearsal that does not end with `overall: "PASS"` found a bug —
file it against the check `id` it failed, not against a table at random.
