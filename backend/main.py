"""
Smart Core Warehouse — backend.

    python -m backend.main            (or: ./run.sh)
    http://localhost:8000

One process does everything: simulated clock, SQLite, the plant model, the MQTT
bridge to the ESP32, the WebSocket push to the dashboard, and serving the
dashboard itself (same origin -> no CORS to debug at 3 a.m.).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from algo import engine as E
from backend import config as C
from backend import db as DB
from backend import dbview as DBVIEW
from backend import plant as PLANT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "dashboard")

app = FastAPI(title="Smart Core Warehouse")
con = DB.connect()
DB.init(con)


# ---------------------------------------------------------------------------
# Simulated clock — the single source of time. No wall clocks anywhere else.
# ---------------------------------------------------------------------------

class SimClock:
    def __init__(self) -> None:
        self.t_sim = C.CLOCK_START_SIM
        self.speed = C.DEFAULT_SPEED
        self._last = time.monotonic()

    def tick(self) -> float:
        now = time.monotonic()
        self.t_sim += (now - self._last) * self.speed
        self._last = now
        return self.t_sim

    def jump(self, hours: float) -> None:
        self.t_sim += hours * 3600.0

    def label(self) -> str:
        d, rem = divmod(int(self.t_sim), 86400)
        h, m = divmod(rem // 60, 60)
        return "J+%d %02d:%02d" % (d, h, m)


clock = SimClock()

# ---------------------------------------------------------------------------
# Volatile state (never persisted — it is all derivable or cosmetic)
# ---------------------------------------------------------------------------

STATE = {
    "env": {"t_c": 24.0, "rh": 52.0},
    "device": {"online": False, "state": "IDLE", "count_beam": 0,
               "gross_g": 0.0, "stable": False, "last_seen_sim": -1e9,
               "source": "none"},
    "crane": {"cmd": "idle", "box_id": None, "slot_id": None, "seq": 0},
    "last_order": None,
    "banner": None,
    "mode": "L0",          # L0 = live ESP32, L1 = backend fallback, L2 = replay
}

CLIENTS: set[WebSocket] = set()
_arrival_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# MQTT bridge (optional — the app runs fine with the broker unreachable)
# ---------------------------------------------------------------------------

class Mqtt:
    def __init__(self) -> None:
        self.ok = False
        self.client = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop) -> None:
        self.loop = loop
        try:
            import paho.mqtt.client as mqtt
        except Exception:
            print("[mqtt] paho-mqtt not installed — running in L1 mode")
            return
        try:
            cid = "scw-backend-%d" % int(time.time() % 100000)
            try:                                     # paho 2.x
                self.client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
            except Exception:                        # paho 1.x
                self.client = mqtt.Client(client_id=cid)
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.connect_async(C.MQTT_HOST, C.MQTT_PORT, 30)
            self.client.loop_start()
            print("[mqtt] connecting to %s:%d  session=%s"
                  % (C.MQTT_HOST, C.MQTT_PORT, C.SESSION))
        except Exception as exc:                     # pragma: no cover
            print("[mqtt] unavailable: %s" % exc)

    def _on_connect(self, client, *_a) -> None:
        self.ok = True
        client.subscribe(C.T_TELEMETRY)
        client.subscribe(C.T_BOX_DONE)
        print("[mqtt] connected, subscribed to %s and %s"
              % (C.T_TELEMETRY, C.T_BOX_DONE))

    def _on_message(self, _c, _u, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        if self.loop:
            self.loop.call_soon_threadsafe(
                self.inbox.put_nowait, (msg.topic, payload))

    def pub(self, topic: str, payload: dict) -> None:
        if self.client is not None:
            try:
                self.client.publish(topic, json.dumps(payload), qos=0)
            except Exception:
                pass


mq = Mqtt()


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------

def box_view(b: dict, now: float) -> dict:
    art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (b["article_ref"],)) or {}
    return {
        "box_id": b["box_id"], "ref": b["article_ref"],
        "label": art.get("label", b["article_ref"]),
        "color": art.get("color", "#888"),
        "qty_initial": b["qty_initial"], "qty_available": b["qty_available"],
        "slot_id": b["slot_id"], "state": b["state"],
        "t_in_sim": b["t_in_sim"], "required_cure_h": round(b["required_cure_h"], 1),
        "ready_at_sim": b["ready_at_sim"],
        "age_h": round(max(0.0, (now - b["t_in_sim"]) / 3600.0), 1),
        "cure_pct": round(E.cure_progress(b["t_in_sim"], b["required_cure_h"], now), 1),
        "h_remaining": round(E.hours_remaining(
            b["t_in_sim"], b["required_cure_h"], now), 1),
        "count_beam": b["count_beam"], "count_weight": b["count_weight"],
        "gross_g": round(b["gross_g"] or 0.0, 1),
        "confidence": b["confidence"], "reason": b["reason"],
        "locked_by": b["locked_by"], "lock_expires_sim": b["lock_expires_sim"],
    }


def snapshot() -> dict:
    now = clock.t_sim
    boxes = DB.rows(con, "SELECT * FROM boxes ORDER BY t_in_sim, box_id")
    views = [box_view(b, now) for b in boxes]
    used = sum(1 for b in boxes if b["slot_id"])
    counts: dict[str, int] = {}
    for b in boxes:
        counts[b["state"]] = counts.get(b["state"], 0) + 1
    dev = dict(STATE["device"])
    dev["online"] = (now - dev["last_seen_sim"]) < 15 * 60      # 15 sim-minutes

    # CDC task 5: "classer les box selon type, quantite et date de stockage".
    # One row per reference, so the jury can read stock by TYPE at a glance,
    # with the oldest cured box of that type named -- that is the FIFO head.
    by_ref = []
    for art in DB.rows(con, "SELECT * FROM articles ORDER BY ref"):
        mine = [v for v in views if v["ref"] == art["ref"]]
        ready = [v for v in mine if v["state"] == "READY"]
        ready.sort(key=lambda v: (v["t_in_sim"], v["box_id"]))
        by_ref.append({
            "ref": art["ref"], "label": art["label"], "color": art["color"],
            "unit_mass_g": art["unit_mass_g"],
            "boxes": len(mine),
            "ready": sum(v["qty_available"] for v in ready),
            "drying": sum(v["qty_available"] for v in mine
                          if v["state"] == "DRYING"),
            "reserved": sum(v["qty_available"] for v in mine
                            if v["state"] == "RESERVED"),
            "quarantine": sum(1 for v in mine if v["state"] == "QUARANTINE"),
            "fifo_head": ready[0]["box_id"] if ready else None,
            "fifo_head_slot": ready[0]["slot_id"] if ready else None,
            "fifo_head_age_h": ready[0]["age_h"] if ready else None,
        })
    return {
        "type": "state",
        "t_sim": round(now, 1),
        "speed": clock.speed,
        "clock_label": clock.label(),
        "mode": STATE["mode"],
        "env": STATE["env"],
        "device": dev,
        "mqtt": mq.ok,
        "banner": STATE["banner"],
        "crane": STATE["crane"],
        "last_order": STATE["last_order"],
        "kpi": {
            "slots_total": C.SLOT_COUNT,
            "slots_used": used,
            "slots_free": C.SLOT_COUNT - used,
            "boxes_ready": counts.get("READY", 0),
            "boxes_drying": counts.get("DRYING", 0),
            "boxes_reserved": counts.get("RESERVED", 0),
            "boxes_quarantine": counts.get("QUARANTINE", 0),
            "cores_available": sum(b["qty_available"] for b in boxes
                                   if b["state"] in ("READY", "RESERVED")),
            "cores_drying": sum(b["qty_available"] for b in boxes
                                if b["state"] == "DRYING"),
        },
        "by_ref": by_ref,
        "boxes": views,
        "slots_occupancy": {b["slot_id"]: b["box_id"]
                            for b in boxes if b["slot_id"]},
    }


async def broadcast(msg: dict | None = None) -> None:
    if not CLIENTS:
        return
    data = json.dumps(msg or snapshot(), ensure_ascii=False)
    dead = []
    for ws in list(CLIENTS):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CLIENTS.discard(ws)


def event(kind: str, payload: dict) -> None:
    DB.log_event(con, clock.t_sim, kind, payload)


# ---------------------------------------------------------------------------
# Storing a box  (called by both the MQTT path and the L1 fallback)
# ---------------------------------------------------------------------------

def store_box(ref: str, count_beam: int, gross_g: float, source: str) -> dict:
    now = clock.t_sim
    art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
    box_id = DB.next_id(con, "boxes", "box_id", "BOX")

    if art is None:
        con.execute(
            "INSERT INTO boxes(box_id,article_ref,qty_initial,qty_available,"
            "slot_id,state,t_in_sim,required_cure_h,ready_at_sim,count_beam,"
            "count_weight,gross_g,confidence,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (box_id, ARTICLE_FALLBACK, 0, 0, None, "QUARANTINE", now, 24.0,
             now + 86400, count_beam, 0, gross_g, "NULLE",
             "reference inconnue: %s" % ref))
        con.commit()
        event("quarantine", {"box_id": box_id, "ref": ref,
                             "reason": "reference inconnue"})
        return {"box_id": box_id, "state": "QUARANTINE"}

    verdict = E.assess_box(art, count_beam, gross_g)
    env = STATE["env"]
    req_h = E.required_cure_h(env["t_c"], env["rh"], art["cure_floor_h"])

    slot = None
    if verdict["accepted"]:
        free = DB.rows(con, "SELECT * FROM slots WHERE occupied_by IS NULL "
                            "AND reserved_for IS NULL")
        slot = E.choose_slot(free, ref)

    state = "DRYING" if (verdict["accepted"] and slot) else verdict["state"]
    if verdict["accepted"] and not slot:
        state, verdict["reason"] = "QUARANTINE", "aucun emplacement libre"

    qty = verdict["quantity"]
    con.execute(
        "INSERT INTO boxes(box_id,article_ref,qty_initial,qty_available,slot_id,"
        "state,t_in_sim,required_cure_h,ready_at_sim,count_beam,count_weight,"
        "gross_g,confidence,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (box_id, ref, qty, qty, slot["slot_id"] if slot else None, state, now,
         req_h, now + req_h * 3600.0, verdict["count_beam"],
         verdict["count_weight"], gross_g, verdict["confidence"],
         verdict.get("reason")))
    if slot:
        con.execute("UPDATE slots SET occupied_by=? WHERE slot_id=?",
                    (box_id, slot["slot_id"]))
    con.commit()

    STATE["crane"] = {"cmd": "store", "box_id": box_id,
                      "slot_id": slot["slot_id"] if slot else None,
                      "seq": STATE["crane"]["seq"] + 1}
    event("box_in", {"box_id": box_id, "ref": ref, "qty": qty,
                     "slot": slot["slot_id"] if slot else None,
                     "state": state, "confidence": verdict["confidence"],
                     "delta": verdict["delta"], "source": source,
                     "required_cure_h": round(req_h, 1)})
    STATE["banner"] = {
        "kind": "quarantine" if state == "QUARANTINE" else "ok",
        "text": (verdict.get("reason") or
                 "%s stocke en %s — %d noyaux, sechage %.1f h"
                 % (box_id, slot["slot_id"] if slot else "-", qty, req_h)),
        "t_sim": now,
    }
    return {"box_id": box_id, "state": state, "slot": slot, **verdict}


ARTICLE_FALLBACK = DB.ARTICLES[0][0]


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def loop_clock() -> None:
    """Advance simulated time and let boxes cure / locks expire."""
    while True:
        now = clock.tick()
        changed = False
        for b in DB.rows(con, "SELECT * FROM boxes WHERE state IN "
                              "('DRYING','RESERVED')"):
            patch = E.tick_box(b, now)
            if patch:
                DB.update(con, "boxes", "box_id", b["box_id"], patch)
                changed = True
                if patch.get("state") == "READY" and b["state"] == "DRYING":
                    event("cured", {"box_id": b["box_id"],
                                    "after_h": round(b["required_cure_h"], 1)})
        if changed:
            con.commit()
        await asyncio.sleep(0.2)


async def loop_ws() -> None:
    while True:
        await broadcast()
        await asyncio.sleep(1.0 / C.WS_HZ)


async def loop_mqtt_in() -> None:
    while True:
        topic, payload = await mq.inbox.get()
        if topic == C.T_TELEMETRY:
            STATE["device"].update({
                "state": payload.get("state", "IDLE"),
                "count_beam": payload.get("count_beam", 0),
                "gross_g": payload.get("gross_g", 0.0),
                "stable": payload.get("stable", False),
                "last_seen_sim": clock.t_sim,
                "source": payload.get("src", "esp32"),
            })
            if "t_c" in payload and payload["t_c"] is not None:
                STATE["env"] = {"t_c": payload["t_c"], "rh": payload["rh"]}
        elif topic == C.T_BOX_DONE:
            STATE["device"]["last_seen_sim"] = clock.t_sim
            res = store_box(payload.get("ref", ""),
                            int(payload.get("count_beam", 0)),
                            float(payload.get("gross_g", 0.0)),
                            source="esp32")
            await broadcast()
            print("[box_done] %s -> %s" % (payload.get("ref"), res["state"]))


_DEVICE_ANSWER = asyncio.Event()


async def run_arrival(ref: str, qty: int, anomaly: str = "none") -> dict:
    """Play one box arrival through the plant model, at 10 Hz.

    L0: the ESP32 is listening, counts, and answers on scw/<S>/dev/box_done.
    L1: no answer within the timeout -> the backend does the ESP32's arithmetic
        itself and stores the box anyway. Identical screen, demo never dies.
    """
    async with _arrival_lock:
        art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
        if art is None:
            return {"error": "unknown ref"}
        frames = PLANT.build_arrival(art, qty, anomaly)

        mq.pub(C.T_CMD, {"cmd": "start_box", "ref": ref})
        _DEVICE_ANSWER.clear()
        before = con.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]

        for f in frames:
            f = dict(f, t_c=STATE["env"]["t_c"], rh=STATE["env"]["rh"],
                     t_sim=round(clock.t_sim, 1))
            mq.pub(C.T_RAW, f)
            STATE["device"]["source"] = "plant"
            await asyncio.sleep(1.0 / C.RAW_HZ)

        # give the board a moment to declare the box
        for _ in range(25):
            await asyncio.sleep(0.1)
            if con.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] > before:
                STATE["mode"] = "L0"
                return {"mode": "L0"}

        # --- L1 fallback: byte-identical outcome, computed here --------------
        STATE["mode"] = "L1"
        beam = sum(1 for i, f in enumerate(frames)
                   if f["beam"] == 0 and (i == 0 or frames[i - 1]["beam"] == 1))
        gross = PLANT.final_gross_g(frames)
        res = store_box(ref, beam, gross, source="L1")
        await broadcast()
        return {"mode": "L1", **{k: v for k, v in res.items() if k != "slot"}}


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state():
    return snapshot()


@app.get("/api/articles")
async def api_articles():
    return DB.rows(con, "SELECT * FROM articles ORDER BY ref")


@app.post("/api/articles")
async def api_articles_add(body: dict):
    """Register a new reference/type from the dashboard's "+ New reference"
    form. Everything downstream (dropdowns, FIFO, the twin) reads articles
    from the DB at runtime, so nothing else needs to know this ran."""
    try:
        art = DB.add_article(
            con, ref=body.get("ref", ""), label=body.get("label", ""),
            unit_mass_g=float(body.get("unit_mass_g", 0) or 0),
            tolerance_g=(float(body["tolerance_g"])
                        if body.get("tolerance_g") not in (None, "") else None),
            box_capacity=int(body.get("box_capacity", 40) or 40),
            cure_floor_h=float(body.get("cure_floor_h", 24.0) or 24.0),
            color=body.get("color") or None)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, 400)
    event("article_new", {"ref": art["ref"], "label": art["label"],
                          "unit_mass_g": art["unit_mass_g"]})
    await broadcast()
    return art


@app.get("/api/slots")
async def api_slots():
    return DB.rows(con, "SELECT * FROM slots ORDER BY face,col,level")


@app.get("/api/anomalies")
async def api_anomalies():
    return PLANT.ANOMALIES


@app.post("/api/clock")
async def api_clock(body: dict):
    if "speed" in body:
        clock.tick()
        clock.speed = max(0.0, float(body["speed"]))
    if "jump_h" in body:
        clock.tick()
        clock.jump(float(body["jump_h"]))
        event("clock_jump", {"hours": body["jump_h"]})
    await broadcast()
    return {"t_sim": clock.t_sim, "speed": clock.speed}


@app.post("/api/sim/env")
async def api_env(body: dict):
    STATE["env"] = {"t_c": float(body.get("t_c", STATE["env"]["t_c"])),
                    "rh": float(body.get("rh", STATE["env"]["rh"]))}
    mq.pub(C.T_CMD, {"cmd": "env", **STATE["env"]})
    event("env", STATE["env"])
    await broadcast()
    return STATE["env"]


@app.post("/api/sim/arrival")
async def api_arrival(body: dict):
    """The demo's main button: a box lands on the conveyor."""
    ref = body.get("ref") or DB.ARTICLES[0][0]
    qty = int(body.get("qty", 37))
    anomaly = body.get("anomaly", "none")
    res = await run_arrival(ref, qty, anomaly)
    await broadcast()
    return res


@app.post("/api/sim/box")
async def api_sim_box(body: dict):
    """L1 shortcut: create a box with no plant model and no ESP32 at all."""
    ref = body.get("ref") or DB.ARTICLES[0][0]
    qty = int(body.get("qty", 37))
    art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
    gross = C.TARE_G + qty * (art["unit_mass_g"] if art else 200.0)
    res = store_box(ref, qty, gross, source="manual")
    await broadcast()
    return {k: v for k, v in res.items() if k != "slot"}


@app.post("/api/sim/raw")
async def api_raw(body: dict):
    """Direct raw injection (used by the 3D twin / manual sliders)."""
    mq.pub(C.T_RAW, {"beam": int(body.get("beam", 1)),
                     "load_mv": int(body.get("load_mv", 0)),
                     "t_c": STATE["env"]["t_c"], "rh": STATE["env"]["rh"],
                     "t_sim": round(clock.t_sim, 1)})
    return {"ok": True}


@app.post("/api/demand")
async def api_demand(body: dict):
    """Criterion 6: production asks for N cores of a reference."""
    ref = body["ref"]
    qty = int(body["qty"])
    now = clock.t_sim
    boxes = DB.rows(con, "SELECT * FROM boxes")
    order_id = DB.next_id(con, "orders", "order_id", "ORD")
    plan = E.fifo_allocate(boxes, ref, qty, now, order_id)

    # reserve what we plan to take, with a real lock and a real expiry
    for p in plan["picks"]:
        DB.update(con, "boxes", "box_id", p["box_id"],
                  {"state": "RESERVED", "locked_by": order_id,
                   "lock_expires_sim": now + E.LOCK_TTL_H * 3600.0})
    con.execute("INSERT INTO orders(order_id,ref,qty_requested,qty_allocated,"
                "status,created_sim,payload) VALUES (?,?,?,?,?,?,?)",
                (order_id, ref, qty, plan["qty_allocated"], plan["status"],
                 now, json.dumps(plan, ensure_ascii=False)))
    con.commit()
    STATE["last_order"] = plan
    if plan["picks"]:
        STATE["crane"] = {"cmd": "pick", "box_id": plan["picks"][0]["box_id"],
                          "slot_id": plan["picks"][0]["slot_id"],
                          "seq": STATE["crane"]["seq"] + 1}
    event("demand", {"order_id": order_id, "ref": ref, "qty": qty,
                     "allocated": plan["qty_allocated"],
                     "picks": [p["box_id"] for p in plan["picks"]],
                     "rejected": len(plan["rejected"])})
    await broadcast()
    return plan


@app.post("/api/demand/confirm")
async def api_confirm(body: dict):
    oid = body["order_id"]
    row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (oid,))
    if not row:
        return JSONResponse({"error": "unknown order"}, 404)
    plan = json.loads(row["payload"])
    for p in plan["picks"]:
        b = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (p["box_id"],))
        if not b:
            continue
        patch = E.apply_pick(b, p["take"])
        DB.update(con, "boxes", "box_id", b["box_id"], patch)
        if patch["state"] == "EMPTY" and b["slot_id"]:
            con.execute("UPDATE slots SET occupied_by=NULL WHERE slot_id=?",
                        (b["slot_id"],))
            DB.update(con, "boxes", "box_id", b["box_id"], {"slot_id": None})
    con.execute("UPDATE orders SET status='DONE' WHERE order_id=?", (oid,))
    con.commit()
    plan["status"] = "DONE"
    STATE["last_order"] = plan
    STATE["banner"] = {"kind": "ok", "text": "%s servie: %d noyaux preleves"
                       % (oid, plan["qty_allocated"]), "t_sim": clock.t_sim}
    event("pick_done", {"order_id": oid, "qty": plan["qty_allocated"]})
    await broadcast()
    return {"ok": True}


@app.post("/api/demand/cancel")
async def api_cancel(body: dict):
    oid = body["order_id"]
    row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (oid,))
    if not row:
        return JSONResponse({"error": "unknown order"}, 404)
    plan = json.loads(row["payload"])
    for p in plan["picks"]:
        DB.update(con, "boxes", "box_id", p["box_id"],
                  {"state": "READY", "locked_by": None, "lock_expires_sim": None})
    con.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (oid,))
    con.commit()
    STATE["last_order"] = None
    event("order_cancel", {"order_id": oid})
    await broadcast()
    return {"ok": True}


@app.get("/api/events")
async def api_events(limit: int = 200):
    rows = DB.rows(con, "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


@app.post("/api/reset")
async def api_reset(body: dict | None = None):
    DB.seed(con)
    clock.t_sim = C.CLOCK_START_SIM
    STATE["env"] = {"t_c": 24.0, "rh": 52.0}
    STATE["last_order"] = None
    STATE["banner"] = {"kind": "ok", "text": "Systeme reinitialise",
                       "t_sim": 0.0}
    STATE["crane"] = {"cmd": "idle", "box_id": None, "slot_id": None, "seq": 0}
    await broadcast()
    return {"ok": True}


@app.post("/api/scenario")
async def api_scenario(body: dict):
    """One button that fills the warehouse with a believable history.

    Press it before the jury arrives so the rack is not empty and FIFO has
    something to be right about.
    """
    name = body.get("name", "demo")
    if name == "demo":
        DB.seed(con)
        clock.t_sim = 0.0
        # 22 C / 45 % RH -> the adaptive model lands exactly on the 24 h floor,
        # so the arithmetic on screen is one the jury can check in their head.
        STATE["env"] = {"t_c": 22.0, "rh": 45.0}
        plan = [("NY-114", 40, 0.0), ("NY-220", 24, 3.0), ("NY-114", 28, 7.0),
                ("NY-075", 55, 11.0), ("NY-114", 40, 19.0), ("NY-330", 12, 22.0)]
        for ref, qty, at_h in plan:
            clock.t_sim = at_h * 3600.0
            art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
            store_box(ref, qty, C.TARE_G + qty * art["unit_mass_g"], "scenario")
        # t = 34 h: BOX-1/2/3 are cured, BOX-4/5/6 are not. That mix is the
        # whole point -- FIFO has something to choose AND something to refuse.
        clock.t_sim = 34.0 * 3600.0
        STATE["banner"] = {"kind": "ok",
                           "text": "Scenario charge: 6 box, 34 h simulees "
                                   "(3 prets, 3 en sechage)",
                           "t_sim": clock.t_sim}
    await broadcast()
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_text(json.dumps(snapshot(), ensure_ascii=False))
    try:
        while True:
            txt = await ws.receive_text()
            try:
                msg = json.loads(txt)
            except Exception:
                continue
            if msg.get("type") == "raw":
                mq.pub(C.T_RAW, {"beam": msg.get("beam", 1),
                                 "load_mv": msg.get("load_mv", 0),
                                 "t_c": STATE["env"]["t_c"],
                                 "rh": STATE["env"]["rh"],
                                 "t_sim": round(clock.t_sim, 1)})
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(ws)


# ---------------------------------------------------------------------------
# Static dashboard (same origin -> no CORS)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=DASH), name="static")
app.include_router(DBVIEW.router, prefix="/api/db", tags=["db"])


@app.get("/")
async def index():
    return FileResponse(os.path.join(DASH, "index.html"))


@app.get("/db")
async def db_explorer():
    return FileResponse(os.path.join(DASH, "db.html"))


@app.on_event("startup")
async def on_start():
    if not DB.rows(con, "SELECT 1 FROM slots LIMIT 1"):
        DB.seed(con)
        print("[db] fresh database seeded")
    loop = asyncio.get_running_loop()
    mq.start(loop)
    asyncio.create_task(loop_clock())
    asyncio.create_task(loop_ws())
    asyncio.create_task(loop_mqtt_in())
    print("[scw] dashboard on http://localhost:8000   session=%s" % C.SESSION)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
