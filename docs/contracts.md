# contracts.md — freeze this before anyone writes code

Owner: **P3 (dashboard / backend / site)**. Everyone codes against this file.
If you change it, announce it out loud and bump the version line.

    CONTRACT VERSION: 1.4

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
        |               raw sensor signals (beam edge, load cell mV)
        v
 [ FastAPI backend ] --MQTT--> scw/sim/raw  ------> [ ESP32 (Wokwi or real) ]
        ^                                             | debounce, tare, stability,
        |                                             | count, delta check
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

The ESP32 is **never told the answer**. It receives raw signals only and derives
count / mass / coherence itself. That is what makes criterion 9 (embedded, 15 pts)
real instead of decorative.

MQTT broker: `broker.hivemq.com:1883` (TCP for Python, 1883 for Wokwi's
`WiFi.begin("Wokwi-GUEST","")`). Every topic is prefixed with a **session id** so
two laptops in the same room do not collide:

    scw/<SESSION>/sim/raw
    scw/<SESSION>/dev/telemetry
    scw/<SESSION>/dev/box_done
    scw/<SESSION>/dev/cmd

`SESSION` is set in `backend/config.py` and in `firmware/sketch.ino`. **They must
match.** Default: `nrw8`. Change it to something unique (e.g. `nrw8-team7`) in the
first 10 minutes at the venue.

---

## 1. MQTT payloads

### 1.1 `scw/<S>/sim/raw` — backend → ESP32, 10 Hz

```json
{ "beam": 1, "load_mv": 1843, "t_c": 24.6, "rh": 52.0, "t_sim": 93600.0 }
```

| field     | type  | meaning                                                        |
|-----------|-------|----------------------------------------------------------------|
| `beam`    | 0/1   | photoelectric barrier: 1 = clear, 0 = obstructed by a core      |
| `load_mv` | int   | load-cell amplifier output, 0..3300 mV (0 mV = 0 g, 3300 = 30 kg)|
| `t_c`     | float | ambient temperature in the curing room, °C                      |
| `rh`      | float | relative humidity, %                                            |
| `t_sim`   | float | simulated clock, seconds. **Never wall clock.**                 |

Mass conversion used by BOTH sides (hard-coded constant, do not change after H2):

    grams = load_mv * (30000.0 / 3300.0)      # 9.0909 g per mV

### 1.2 `scw/<S>/dev/telemetry` — ESP32 → backend, 2 Hz

```json
{ "state":"COUNTING", "count_beam":37, "gross_g":9420.5,
  "stable":true, "t_c":24.6, "rh":52.0, "up_ms":128400 }
```

`state` ∈ `IDLE | COUNTING | STABILIZING | DONE | FAULT`.
This is telemetry only — it drives the "live ESP32" panel on the dashboard.
It never mutates the database.

### 1.3 `scw/<S>/dev/box_done` — ESP32 → backend, once per box

**This is the only message that creates a box.**

```json
{ "ref":"NY-114", "count_beam":37, "gross_g":9420.5,
  "t_c":24.6, "rh":52.0, "fw":"1.0" }
```

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
| POST   | `/api/sim/arrival`    | `{"ref":"NY-114","qty":37,"anomaly":"none"}` | plays the plant model at 10 Hz, then L0/L1 as in §0 — this is the **A** hotkey |
| POST   | `/api/demand`         | `{"ref":"NY-114","qty":40}`            | allocation plan (§4), or `{"error":...}` (400: missing/invalid `ref`/`qty`) |
| POST   | `/api/demand/confirm` | `{"order_id":"ORD-3"}`                 | `{"ok":true,"already":false,"qty_allocated":N}` — confirming an already-DONE order returns `{"ok":true,"already":true}` instead of deducting again; confirming a CANCELLED/IMPOSSIBLE/unknown order is `{"error":...}` (409/404) |
| POST   | `/api/demand/cancel`  | `{"order_id":"ORD-3"}`                 | `{"ok":true,"already":false,"released":[box_id,...]}` — cancelling an already-CANCELLED order returns `{"ok":true,"already":true}`; cancelling a DONE/IMPOSSIBLE/unknown order is `{"error":...}` (409/404) |
| POST   | `/api/sim/raw`        | `{"beam":0,"load_mv":1843}`            | `{ok:true}` — plant model → MQTT |
| POST   | `/api/sim/box`        | `{"ref":"NY-114","qty":37}`            | L1 FALLBACK: create a box without the ESP32 |
| POST   | `/api/sim/env`        | `{"t_c":31.0,"rh":78.0}`               | force curing-room climate — display/evidence only, contract 1.2 (§6.2) |
| POST   | `/api/reset`          | `{"seed":true}` (default) or `{"seed":false}` | wipe + reseed everything, or (with `seed:false`) wipe boxes/orders/events/slots but keep the current `articles` |
| POST   | `/api/scenario`       | `{"name":"demo"}`                      | loads the rehearsed 6-box / 34 h demo history (full reseed) — the **S** hotkey |
| GET    | `/api/events?limit=200` | –                                    | event log |
| GET    | `/api/db/check`       | –                                      | read-only consistency report (§7 below) |
| GET    | `/api/db/box/{id}`    | –                                      | one box's row + slot + every event/order that names it |

`POST /api/articles` — `ref`, `label`, `unit_mass_g` are required; `tolerance_g`
(default ~3 % of `unit_mass_g`), `box_capacity` (default 40) and `color`
(default: next unused colour from a fixed palette) are optional. A `cure_floor_h`
in the body is accepted but **ignored** — contract 1.2 fixes drying at 24 h for
every reference, so there is no per-reference exception to request. A duplicate
`ref` or a non-positive `unit_mass_g` is a 400. This is how
`dashboard/index.html`'s "+ New reference" form (in the ⋯ menu) adds a type
without a restart — it is additive only, so `backend/db.py::ARTICLES` still
owns the four references the rehearsed demo depends on.

Static: `GET /` serves `dashboard/index.html`. Everything under `/static/*` is
the `dashboard/` folder.

---

## 3. WebSocket `/ws`

Server → client, one JSON object per frame, ~5 Hz:

```json
{
  "type": "state",
  "contract_version": "1.2",
  "t_sim": 93600.0,
  "speed": 60,
  "clock_label": "J+1 02:00",
  "env": {"t_c":24.6,"rh":52.0},
  "device": {"online":true,"state":"COUNTING","count_beam":37,
             "gross_g":9420.5,"last_seen_sim":93598.0},
  "kpi": {"slots_total":306,"slots_used":37,"boxes_ready":12,
          "boxes_drying":9,"boxes_quarantine":1,"cores_available":431},
  "boxes": [ /* see below */ ],
  "orders_pending": [ {"order_id":"ORD-3","ref":"NY-114","qty_requested":40,
                       "qty_allocated":40,"lock_expires_sim":97200.0,
                       "lock_remaining_s":1800.0} ],
  "slots_occupancy": {"F0-C3-L7":"BOX-12", ...},
  "last_order": { /* §4 */ },
  "crane": {"cmd":"store","box_id":"BOX-12","slot_id":"F0-C3-L7"}
}
```

`orders_pending` lists every `PENDING` order (not only `last_order`), with
its lock countdown — added in contract 1.2 so the HMI can show a
reservation about to expire before it happens (§6.5).

Box object (this exact shape is what the 3D twin and the table both read):

```json
{ "box_id":"BOX-12", "ref":"NY-114", "label":"Noyau culasse 114",
  "qty_initial":37, "qty_available":37, "slot_id":"F0-C3-L7",
  "state":"DRYING", "t_in_sim":7200.0, "required_cure_h":26.4,
  "ready_at_sim":102240.0, "cure_pct":38.5,
  "count_beam":37, "count_weight":37, "gross_g":9420.5,
  "confidence":"HAUTE", "reason":null,
  "locked_by":null, "lock_expires_sim":null }
```

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
  "qty_requested": 40,
  "qty_allocated": 40,
  "shortfall": 0,
  "picks": [
    {"box_id":"BOX-4","slot_id":"F0-C1-L2","take":22,"t_in_sim":3600.0,"rank":1},
    {"box_id":"BOX-9","slot_id":"F1-C5-L9","take":18,"t_in_sim":9000.0,"rank":2}
  ],
  "rejected": [
    {"box_id":"BOX-7","reason":"sechage insuffisant","detail":"pret dans 4.2 h"},
    {"box_id":"BOX-2","reason":"quarantaine","detail":"ecart de comptage = 3 (barriere 30 / pesee 27)"},
    {"box_id":"BOX-11","reason":"reserve","detail":"ORD-2"},
    {"box_id":"BOX-15","reason":"plus recent (FIFO)","detail":"besoin deja couvert par des box plus anciens"}
  ],
  "status": "PENDING"
}
```

Each `take` is always the picked box's **entire** `qty_available` (§6.7) —
`fifo_allocate` never splits a box, so `qty_allocated` can land above
`qty_requested` when the last whole box needed to cover the order is bigger
than what was still missing. If the ref's total whole-box stock can't reach
`qty_requested` at all, `picks` is `[]`, `status` is `"IMPOSSIBLE"`, and every
otherwise-pickable box appears in `rejected[]` with reason
`"stock insuffisant"` (detail: `"N disponible(s) au total pour M demande(s)"`).

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
         box_capacity INT, cure_floor_h REAL DEFAULT 24.0, color TEXT)

boxes(box_id PK, article_ref FK NULL, qty_initial INT, qty_available INT,
      slot_id FK NULL, state TEXT, t_in_sim REAL, required_cure_h REAL,
      ready_at_sim REAL, count_beam INT, count_weight INT, gross_g REAL,
      confidence TEXT, reason TEXT, locked_by TEXT NULL,
      lock_expires_sim REAL NULL)
  -- article_ref is NULL only while state='QUARANTINE' (unknown reference,
  -- contract 1.2). A partial unique index on slot_id (WHERE NOT NULL)
  -- makes "one box per slot" DB-enforced, not just convention.

slots(slot_id PK, face INT, col INT, level INT,
      occupied_by FK NULL, reserved_for FK NULL)
  -- occupied_by/reserved_for are soft references (see database-guide.md),
  -- but a partial unique index on occupied_by (WHERE NOT NULL) makes "one
  -- slot per box" DB-enforced. Both are maintained by backend/warehouse.py
  -- in the same transaction as the box they describe.

orders(order_id PK, ref, qty_requested INT, qty_allocated INT,
       status TEXT, created_sim REAL, payload TEXT)
  -- payload.status is kept equal to the status column on every write.

events(id PK AUTOINCREMENT, t_sim REAL, kind TEXT, payload TEXT)
  -- inserted in the SAME transaction as the mutation it describes
  -- (backend/db.py::log_event no longer commits on its own).

meta(k PK, v)
  -- holds the simulated-clock checkpoint (k='t_sim', k='speed'), restored
  -- on backend startup so a restart does not rewind time (contract 1.2).
```

**Persisted states** — what `boxes.state` actually holds:
`DRYING → READY → RESERVED → (partial → READY, t_in unchanged | full →
EMPTY) → ARCHIVED`, plus `QUARANTINE` (a box is born directly into
`DRYING` or `QUARANTINE`; `ARCHIVED` has no endpoint yet).

`INCOMING → IDENTIFYING → COUNTING → STORING` and `PICKING` are **virtual**
stages — real-world moments narrated in event payloads (`box_in`'s
evidence, `pick_done`'s per-box detail) but never written to `boxes.state`.
The whole reserve-then-pick sequence is one atomic backend operation, not
two, so `PICKING` is never observed mid-flight. See `algo/engine.py`'s
`PERSISTED_STATES`/`_TRANSITIONS` and `docs/database-guide.md`.

`orders.status`: `PENDING → DONE | CANCELLED`, or born straight into
`IMPOSSIBLE` (zero picks). Confirming/cancelling an order already in its
target state is a no-op (`{"already": true}`); confirming/cancelling from
any other state is a 409. An expired reservation also lands on
`CANCELLED` (event kind `order_expired`, distinct from a manual
`order_cancel`).

---

## 6. Rules that are not negotiable

1. **No wall-clock timestamps anywhere.** Everything is `t_sim` in seconds.
   If you ever write `datetime.now()` into the DB, the 24 h demo breaks.
   (The clock checkpoint in `meta` and device-liveness checks legitimately
   use `time.monotonic()`, but only in backend runtime memory — never
   written to a warehouse column.)
2. `required_cure_h = 24.0`, always, for every box and every reference —
   **fixed, not adaptive** (contract 1.2). `t_c`/`rh` are recorded on each
   box as arrival evidence and shown on the HMI, but never computed with.
3. FIFO sort key is `(t_in_sim, box_id)`, and the `box_id` tiebreak is
   compared **numerically** (`algo.engine.fifo_key`), not as a string —
   `BOX-2` sorts before `BOX-10`. Every place that orders boxes for FIFO
   purposes (allocation, the inventory table, the by-ref FIFO head) uses
   this same key.
4. `apply_pick` (confirm) supports a partial take in principle and returns
   the box to `READY` with **`t_in_sim` unchanged** if it ever gets one —
   re-stamping it would silently break FIFO. In practice this never fires
   today, because rule 7 means `fifo_allocate` never generates a partial
   take.
5. `RESERVED` is a real lock with `lock_expires_sim`; expiry releases the
   box **and cancels its order** (`order_expired` event) — a lock running
   out never leaves a `PENDING` order pointing at boxes that already moved
   on.
6. Confirm and cancel are **idempotent, not re-appliable**: repeating either
   on an order already in its target state is a no-op; applying either to
   an order in the wrong state is refused (409), never silently applied to
   whatever the boxes happen to be now.
7. **A pick never splits a box** (contract 1.3): `take` is always a box's
   whole `qty_available`. If the ref's total whole-box stock can't reach
   `qty_requested`, the reservation is refused outright (`IMPOSSIBLE`,
   `picks: []`) rather than reserving less than what was asked for.
7. Every backend operation that touches more than one row runs inside one
   database transaction (`backend/db.py::transaction`, used throughout
   `backend/warehouse.py`) — a crash or a refused precondition leaves
   nothing half-written.
8. All UI strings come from `dashboard/labels.js`. Nobody hard-codes a string
   in the HTML. Language switch = one line.

---

## 7. Consistency checker

`GET /api/db/check` (`backend/consistency.py`) runs 22 read-only checks —
foreign-key-style integrity, box/order state shape, lock consistency, cure
timing, article sanity — and returns `{"overall": "PASS"|"WARN"|"FAIL",
"t_sim", "checks": [{"id","severity","count","offending","message"}]}`.
It is exposed as a badge on the DB Explorer (`/db`). A demo, a chaos test,
or a rehearsal that does not end with `overall: "PASS"` found a bug —
file it against the check `id` it failed, not against a table at random.
