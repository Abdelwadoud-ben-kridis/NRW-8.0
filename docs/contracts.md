# contracts.md — freeze this before anyone writes code

Owner: **P3 (dashboard / backend / site)**. Everyone codes against this file.
If you change it, announce it out loud and bump the version line.

    CONTRACT VERSION: 1.1

---

## 0. Topology

```
 [ 3D twin in browser ]  = PLANT MODEL (the physical world)
        |  WS  /ws        raw sensor signals (beam edge, load cell mV)
        v
 [ FastAPI backend ] --MQTT--> scw/sim/raw  ------> [ ESP32 (Wokwi or real) ]
        ^                                             | debounce, tare, stability,
        |                                             | count, delta check
        +---MQTT--- scw/dev/telemetry ----------------+
        +---MQTT--- scw/dev/box_done  ----------------+
        |
   [ SQLite ] <-- algo/engine.py (pure functions, no I/O)
        |
        +--WS--> dashboard (HMI + 3D twin) 5 Hz state push
```

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
`QUARANTINE` with reason `reference inconnue`.

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
| POST   | `/api/clock`          | `{"speed":60}` or `{"jump_h":6}`       | `{t_sim, speed}` |
| POST   | `/api/demand`         | `{"ref":"NY-114","qty":40}`            | allocation plan (§4) |
| POST   | `/api/demand/confirm` | `{"order_id":"ORD-3"}`                 | `{ok:true}` |
| POST   | `/api/demand/cancel`  | `{"order_id":"ORD-3"}`                 | `{ok:true}` |
| POST   | `/api/sim/raw`        | `{"beam":0,"load_mv":1843}`            | `{ok:true}` — plant model → MQTT |
| POST   | `/api/sim/box`        | `{"ref":"NY-114","qty":37}`            | L1 FALLBACK: create a box without the ESP32 |
| POST   | `/api/sim/env`        | `{"t_c":31.0,"rh":78.0}`               | force curing-room climate |
| POST   | `/api/reset`          | `{"seed":true}`                        | wipe + reseed |
| GET    | `/api/events?limit=200` | –                                    | event log (L2 replay source) |
| POST   | `/api/replay`         | `{"speed":8}`                          | L2 FALLBACK: replay events through WS |

`POST /api/articles` — `ref`, `label`, `unit_mass_g` are required; `tolerance_g`
(default ~3 % of `unit_mass_g`), `box_capacity` (default 40), `cure_floor_h`
(default 24.0) and `color` (default: next unused colour from a fixed palette)
are optional. A duplicate `ref` or a non-positive `unit_mass_g` is a 400. This
is how `dashboard/index.html`'s "+ New reference" form (in the ⋯ menu) adds a
type without a restart — it is additive only, so `backend/db.py::ARTICLES`
still owns the four references the rehearsed demo depends on.

Static: `GET /` serves `dashboard/index.html`. Everything under `/static/*` is
the `dashboard/` folder.

---

## 3. WebSocket `/ws`

Server → client, one JSON object per frame, ~5 Hz:

```json
{
  "type": "state",
  "t_sim": 93600.0,
  "speed": 60,
  "clock_label": "J+1 02:00",
  "env": {"t_c":24.6,"rh":52.0},
  "device": {"online":true,"state":"COUNTING","count_beam":37,
             "gross_g":9420.5,"last_seen_sim":93598.0},
  "kpi": {"slots_total":306,"slots_used":37,"boxes_ready":12,
          "boxes_drying":9,"boxes_quarantine":1,"cores_available":431},
  "boxes": [ /* see below */ ],
  "slots_occupancy": {"F0-C3-L7":"BOX-12", ...},
  "last_order": { /* §4 */ },
  "crane": {"cmd":"store","box_id":"BOX-12","slot_id":"F0-C3-L7"}
}
```

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
    {"box_id":"BOX-7","reason":"séchage insuffisant","detail":"prêt dans 4.2 h"},
    {"box_id":"BOX-2","reason":"quarantaine","detail":"écart comptage = 3"},
    {"box_id":"BOX-11","reason":"réservé","detail":"ORD-2"},
    {"box_id":"BOX-15","reason":"plus récent (FIFO)","detail":"t_in postérieur"}
  ],
  "status": "PENDING"
}
```

**The `rejected` list is not optional.** It is how the jury *sees* FIFO being
enforced instead of taking your word for it. Render it on screen, always.

---

## 5. Database (SQLite, WAL)

```sql
articles(ref PK, label, unit_mass_g REAL, tolerance_g REAL,
         box_capacity INT, cure_floor_h REAL DEFAULT 24.0)

boxes(box_id PK, article_ref FK, qty_initial INT, qty_available INT,
      slot_id FK NULL, state TEXT, t_in_sim REAL, required_cure_h REAL,
      ready_at_sim REAL, count_beam INT, count_weight INT, gross_g REAL,
      confidence TEXT, reason TEXT, locked_by TEXT NULL,
      lock_expires_sim REAL NULL)

slots(slot_id PK, face INT, col INT, level INT,
      occupied_by FK NULL, reserved_for FK NULL)

orders(order_id PK, ref, qty_requested INT, qty_allocated INT,
       status TEXT, created_sim REAL, payload TEXT)

events(id PK AUTOINCREMENT, t_sim REAL, kind TEXT, payload TEXT)
```

States: `INCOMING → IDENTIFYING → COUNTING → STORING → DRYING → READY →
RESERVED → PICKING → (partial → READY, t_in unchanged) → EMPTY → ARCHIVED`,
plus `QUARANTINE` reachable from `COUNTING`.

---

## 6. Rules that are not negotiable

1. **No wall-clock timestamps anywhere.** Everything is `t_sim` in seconds.
   If you ever write `datetime.now()` into the DB, the 24 h demo breaks.
2. `required_cure_h = max(24.0, model(T, RH))` — the floor is never violated.
3. FIFO sort key is `(t_in_sim, box_id)` — the `box_id` tiebreak makes it
   deterministic, which matters when you demo twice.
4. A partial pick returns the box to `READY` with **`t_in_sim` unchanged**.
   Re-stamping it would silently break FIFO.
5. `RESERVED` is a real lock with `lock_expires_sim`; expiry releases it.
6. All UI strings come from `dashboard/labels.js`. Nobody hard-codes a string
   in the HTML. Language switch = one line.
