# Smart Core Warehouse — P3 (dashboard, backend & site), 0 → done

NRW 8.0 @ INSAT · défi SOPAL & SOPALTEC · 160 points.
**This repo already runs.** Nothing in it is a sketch or a TODO.

You own the busiest role: the backend, the WebSocket, the HMI, the 3D twin, and
the keyboard during the demo. Everything below is written for that job.

---

## 0. Sixty seconds to a running system

```bash
cd scw
pip install -r requirements.txt      # fastapi, uvicorn[standard], paho-mqtt
./run.sh                             # or: python -m uvicorn backend.main:app --port 8000
```

Open **http://localhost:8000**, press **S** (load demo scenario), press **D**
(production demand). You now have the full scored demo on screen. That is the
whole system; everything after this point is explanation.

Second terminal, optional but recommended — a Python stand-in for the ESP32 so
you are never blocked by Wokwi:

```bash
python tools/fake_device.py
```

**The one thing to do in your first ten minutes at the venue:** open
`backend/config.py`, change `SESSION = "nrw8"` to something unique like
`"nrw8-teamX"`, and make the identical change to `SESSION` in
`firmware/sketch.ino`. You are on a public MQTT broker; another team on
`nrw8` will inject phantom boxes into your demo.

### Two traps that will cost you an hour if you hit them cold

1. `uvicorn` installed **without** a websocket library answers `/ws` with a 404
   and the dashboard silently freezes at `J+0 00:00`. `pip install
   "uvicorn[standard]"` fixes it. The dashboard also falls back to polling on
   its own after 2.5 s, so it will still work — but slower, and you will waste
   time wondering why.
2. Three.js is **vendored** in `dashboard/vendor/`. Do not "clean it up" into a
   CDN link. Venue wifi is exactly the thing that must not be able to break
   your 25-point criterion.

---

## 1. What the CDC asks, and where each answer lives

| # | Task (CDC §4) | Points | Where it is |
|---|----------------|--------|-------------|
| 1 | Identify the core / its model | 15 | `algo/engine.py::assess_box` — unit-mass check against the declared reference |
| 2 | Deduce the quantity | 15 | same function — barrier count × mass count, cross-checked |
| 3 | Register a new box, automatic timestamp | — | `backend/main.py::store_box`, `t_in_sim` |
| 4 | Track drying, ready / not ready at 24 h | 10 | `engine.required_cure_h` + `engine.tick_box` |
| 5 | Classify and locate by type, quantity, storage date | 15 | `by_ref` panel + `slots` table + inventory `AGE` column |
| 6 | Automatically propose the right box | 15 | `engine.fifo_allocate` → the proposal panel |
| 7 | Mechanical design **and** animated 3D | **25** | `dashboard/twin.js` |
| 8 | Real-time interactive dashboard | 15 | `dashboard/index.html` + `app.js` |
| 9 | Full embedded simulation (ESP32) | 15 | `firmware/sketch.ino` on Wokwi |
| 10 | An innovative idea | 10 | adaptive cure index from live T/RH, floored at 24 h |
| 11 | Clear presentation, fluid demo | 15 | `docs/demo-script.md` |

The CDC's own sentence — *"the system must be able to say at any moment: which
core type is present, how many, in which box, for how long, whether they are
ready, and which box to use first"* — maps to six columns of one table:
`REF · QTY · BOX · AGE · CURE/STATE · the proposal panel`. That table is the
literal answer to the brief. Do not let it get buried.

---

## 2. The shape of the system

```
  browser ────────────────────────────────────────────┐
   dashboard/index.html                               │
     ├─ app.js     HMI: KPIs, stock by ref, proposal, │ WebSocket /ws  (5 Hz)
     │             inventory, event log               │ + REST /api/*
     └─ twin.js    Three.js digital twin              │
                                                      ▼
                                         backend/main.py  (FastAPI)
                                           ├─ SimClock      simulated time
                                           ├─ plant.py      raw signal generator
                                           ├─ db.py         SQLite (WAL)
                                           └─ algo/engine.py  ALL the decisions
                                                      │
                                              MQTT (public broker)
                                                      │
                     scw/<SESSION>/sim/raw  ──────────┼──────────▶  ESP32
                     scw/<SESSION>/dev/telemetry ◀────┤            (Wokwi
                     scw/<SESSION>/dev/box_done  ◀────┘             or real)
```

Three rules hold the whole thing together:

1. **The ESP32 is never told the answer.** It receives a beam bit and a
   millivolt reading. It derives the count itself. That separation is what
   makes criterion 9 defensible when a juror pushes on it.
2. **No wall-clock timestamps, anywhere.** Everything is `t_sim` in seconds.
   One `datetime.now()` in the database and the 24-hour demo stops working.
3. **All decisions live in `algo/engine.py`,** which imports nothing and does
   no I/O. That is why it has 20 unit tests that run in 30 ms, and why you can
   answer "what would happen if…" on a whiteboard.

---

## 3. File by file

```
scw/
├── run.sh                  one command to start everything
├── requirements.txt
├── docs/
│   ├── contracts.md        ← FREEZE THIS FIRST. Topics, payloads, REST, WS, DB.
│   ├── demo-script.md      the 7-minute demo, hotkeys, fallbacks, hostile Q&A
│   └── database-guide.md   how to browse/query scw.db: DB Explorer, sqlite3, Python
├── algo/
│   ├── engine.py           every decision. No I/O. ~330 lines.
│   └── test_engine.py      20 tests, one per scored behaviour
├── backend/
│   ├── config.py           SESSION, broker, rack geometry, physics constants
│   ├── db.py               schema, seeding, 306-slot rack generation
│   ├── plant.py            the pretend physical world: raw signals + anomalies
│   └── main.py             clock, REST, WebSocket, MQTT bridge, static serving
├── dashboard/
│   ├── index.html          structure only — no text, no logic
│   ├── labels.js           EVERY user-visible string. FR/EN switch = one line.
│   ├── style.css
│   ├── app.js              HMI + WebSocket + hotkeys
│   ├── twin.js             Three.js twin, procedural rack, crane animation
│   ├── vendor/             three.js r160, vendored — do not replace with a CDN
│   └── models/             drop P1's crane.glb / fork.glb / crate.glb here
├── firmware/
│   ├── sketch.ino          ESP32 — same file for Wokwi and the real board
│   ├── diagram.json        Wokwi wiring
│   └── libraries.txt       PubSubClient · ArduinoJson · DHT sensor library
└── tools/
    ├── fake_device.py      Python ESP32 stand-in, byte-identical payloads
    └── smoke.py            19 end-to-end checks against a running backend
```

---

## 4. The three files you will actually edit

### `dashboard/labels.js`
The jury's language is decided at the door. Last line of the file:

```js
export const L = EN;      // change to FR and refresh. That is the whole change.
```

Never type a user-visible string anywhere else. If you catch yourself writing
`"Quantité"` in `index.html`, stop and put it here.

### `backend/config.py`
`SESSION`, the broker, the rack geometry, and the millivolt-to-gram constant
that **must** match `G_PER_MV` in the firmware. Change the rack dimensions here
and the 3D regenerates — but change them in `dashboard/twin.js::GEO` too; those
two are deliberately duplicated so the twin still draws with the backend down.

### `algo/engine.py`
P2's territory. If you touch it, run `python algo/test_engine.py` before you
commit. Twenty tests, half a second. There is no excuse.

---

## 5. Dropping in P1's CAD

The rack is **not** CAD — it is generated from the slots table, which is why it
cost zero modelling hours. P1 only needs four parts. Export each from Fusion as
glTF binary and drop it in `dashboard/models/`:

```
crane.glb      the mast/column          fork.glb      the telescopic fork
crate.glb      the 51.5×32.5×17.5 cm crate  conveyor.glb  the infeed conveyor
```

They load automatically on refresh. If a file is missing the scene uses a
primitive and says so in the console — so the twin is demo-ready from hour one
and only gets prettier as P1 delivers. Orient the models Y-up, metres, origin
at the part's own mounting point.

---

## 6. Running the whole loop with the ESP32

1. Open [wokwi.com](https://wokwi.com) → new ESP32 project.
2. Paste `firmware/diagram.json` into the **diagram.json** tab.
3. Paste `firmware/sketch.ino` into **sketch.ino**.
4. Library Manager → add the three from `libraries.txt`.
5. Check `SESSION` matches `backend/config.py`. Start the simulation.
6. The ESP32 pill in the dashboard turns green. Press **A**.

Wokwi's `Wokwi-GUEST` network needs no password. For P5's real board, change
the SSID/password in `setup()` and nothing else — same firmware, same topics.

---

## 7. Testing

```bash
python algo/test_engine.py      # 20 unit tests, no server needed
python tools/smoke.py           # 19 end-to-end checks, backend must be running
```

`smoke.py` walks the exact demo path: two boxes arrive five simulated hours
apart, a demand before curing is refused **with reasons**, the clock jumps, the
boxes cure on their own, FIFO allocates across two boxes oldest-first, a
partial pick keeps `t_in_sim` unchanged, the emptied box releases its slot, an
injected anomaly lands in quarantine, and a cold humid room extends the cure.

Run both after every merge. Run both again at H23, before the feature freeze.

---

## 8. Where the 25 points are, and what to do with spare hours

Criterion 7 (animated 3D) is worth more than any other single item, and the
rubric says so out loud. The twin already renders the room, the rack, the
crane, the crates, four camera presets, a follow-camera and a dimension HUD.
Spare hours go here, in this order:

1. P1's real GLB parts (biggest visual jump per hour)
2. Crane motion polish — the fork carrying the crate rather than the crate
   teleporting to its slot
3. Slot-level colour legend on screen, so the colours are self-explanatory
4. A camera move that sweeps the aisle on demand, for the opening beat

What **not** to build, whatever the temptation: computer vision, React, a
bundler, the rack in CAD, authentication, Docker, a mobile app.

---

## 9. Hour-by-hour, from wherever you are now

Since you are already at the event, read this as a priority order, not a clock.

| Priority | Do this | Done when |
|---|---|---|
| 1 | `./run.sh`, open the dashboard, press S then D | you have seen the FIFO proposal with its rejection list |
| 2 | Change `SESSION` in both files; commit | `git log` shows one commit called "demo-stable" |
| 3 | Send `docs/contracts.md` to P2 and P4 | both have read it and said yes |
| 4 | Get Wokwi up with P4 and press **A** | the ESP32 pill is green and the box lands via `box_done` |
| 5 | Run both test suites | two ALL GREENs |
| 6 | Drop in whatever GLB P1 has, even a rough one | the twin shows a real crane |
| 7 | Rehearse `docs/demo-script.md` end to end | five clean runs, one of them with Wokwi deliberately killed |
| 8 | FEATURE FREEZE | nobody touches code; only slides and rehearsal after this |

Priority 7 is not optional and it is not the thing to cut when you run late. A
system that works and a team that fumbles the demo scores worse than the
reverse, because 55 of the 160 points are things the jury has to *see happen*.

---

## 10. Things that are true and that you should be able to say out loud

- 306 slots in a 6 × 6 × 6 m room: 2 faces × 9 columns × 17 levels, sized for a
  51.5 × 32.5 × 17.5 cm crate with ~10 cm clearance per slot on both axes,
  using 5.6 m of length and 4.8 m of height, leaving a 1.2 m top clearance and
  a walkable aisle.
- Worst-case crane cycle is about 10 s at 1.2 m/s travel and 0.8 m/s lift, with
  an S-curve profile because pre-cure cores break if you handle them roughly.
- The cure floor is 24 h and the adaptive model can only extend it: +1.2 % per
  point of RH above 45 %, +2.0 % per °C below 22. At 16 °C and 80 % RH it asks
  for 38.2 h. At 35 °C and 10 % RH it still asks for 24.0 h.
- Two independent counts. Agreement → HAUTE. Off by one → accepted at the lower
  figure, MOYENNE. Off by two or more → quarantine, no guessing.
- FIFO is sorted on `(t_in_sim, box_id)`, so the same demand gives the same
  answer twice — which matters when the jury asks you to run it again.
