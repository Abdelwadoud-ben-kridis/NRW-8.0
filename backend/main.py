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
import secrets
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
from backend import warehouse as W

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
               "last_seen_mono": -1e9, "source": "none"},
    "crane": {"cmd": "idle", "box_id": None, "slot_id": None, "seq": 0},
    "last_order": None,
    "banner": None,
    "mode": "L0",          # L0 = live ESP32, L1 = backend fallback, L2 = replay
}

CLIENTS: set[WebSocket] = set()
_arrival_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Arrival window / dedup (runtime-only -- see algo.engine.dedup_verdict and
# docs/contracts.md CONTRACT VERSION 1.2 §1.3). `_epoch` is bumped by reset
# and scenario-load so an in-flight run_arrival() from before either one
# aborts instead of storing a box into the freshly wiped database.
# ---------------------------------------------------------------------------
_epoch = 0
_arrival_window: dict | None = None
_last_unsolicited: tuple | None = None


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
    # article_ref is NULL for an unknown-reference quarantine box (contract
    # 1.2) -- it no longer gets misfiled under a fallback article, so there
    # is genuinely no article row to join here.
    art = (DB.one(con, "SELECT * FROM articles WHERE ref=?", (b["article_ref"],))
          if b["article_ref"] else None) or {}
    return {
        "box_id": b["box_id"], "ref": b["article_ref"],
        "label": art.get("label") or (b["reason"] if not b["article_ref"] else b["article_ref"]),
        "color": art.get("color", "#888"),
        "code": b["code"],
        "batch_id": b["batch_id"],
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
        "vision_ref": b["vision_ref"], "id_confidence": b["id_confidence"],
        "locked_by": b["locked_by"], "lock_expires_sim": b["lock_expires_sim"],
    }


def snapshot() -> dict:
    now = clock.t_sim
    # ORDER BY t_in_sim only -- box_id's numeric tiebreak (E.fifo_key) cannot
    # be expressed in plain SQL ("BOX-10" < "BOX-2" lexically), so the final
    # deterministic order is applied in Python, once, here -- the same key
    # FIFO allocation uses, so the inventory table, the by-ref FIFO head and
    # the actual allocation never disagree about what "oldest" means.
    boxes = DB.rows(con, "SELECT * FROM boxes ORDER BY t_in_sim")
    boxes.sort(key=E.fifo_key)
    views = [box_view(b, now) for b in boxes]
    used = sum(1 for b in boxes if b["slot_id"])
    counts: dict[str, int] = {}
    for b in boxes:
        counts[b["state"]] = counts.get(b["state"], 0) + 1
    dev = dict(STATE["device"])
    # Liveness is transport-domain (monotonic), not simulated time -- the old
    # "15 sim-minutes" check was wrong at pause (speed 0, stuck "online"
    # forever), at x3600 (flickers every real 25ms) and right after a reset
    # (stale "online" until the next telemetry frame catches up in sim time).
    dev["online"] = (time.monotonic() - dev.get("last_seen_mono", -1e9)) < C.DEVICE_LIVENESS_S

    # CDC task 5: "classer les box selon type, quantite et date de stockage".
    # One row per reference, so the jury can read stock by TYPE at a glance,
    # with the oldest cured box of that type named -- that is the FIFO head.
    by_ref = []
    for art in DB.rows(con, "SELECT * FROM articles ORDER BY ref"):
        mine = [v for v in views if v["ref"] == art["ref"]]
        # A box tagged to someone else's production batch is not general
        # stock (finding: it used to count toward "ready" here even though
        # fifo_allocate/reserve() already refuse to hand it to a new
        # demand -- the KPI and the actual allocation must agree).
        ready = [v for v in mine if v["state"] == "READY" and not v["batch_id"]]
        ready.sort(key=E.fifo_key)
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

    # Every PENDING order, with its lock countdown -- so the HMI can show
    # (and the presenter can see coming) a reservation about to expire,
    # instead of only ever showing the single last_order pointer.
    orders_pending = []
    for o in DB.rows(con, "SELECT * FROM orders WHERE status='PENDING' "
                          "ORDER BY created_sim"):
        plan_o = json.loads(o["payload"])
        lock_exp = plan_o["picks"][0]["lock_expires_sim"] if plan_o.get("picks") else None
        orders_pending.append({
            "order_id": o["order_id"], "ref": o["ref"],
            "qty_requested": o["qty_requested"], "qty_allocated": o["qty_allocated"],
            "lock_expires_sim": lock_exp,
            "lock_remaining_s": round(lock_exp - now, 1) if lock_exp is not None else None,
        })

    # Every open production batch -- existing FIFO stock couldn't cover the
    # order, so it's being made to order instead (backend/warehouse.py::
    # reserve). Live-recomputed from boxes.batch_id, never cached, so the
    # dashboard's progress bar can never drift from what actually cured.
    batches_pending = []
    for o in DB.rows(con, "SELECT * FROM orders WHERE status='IN_PRODUCTION' "
                          "ORDER BY created_sim"):
        plan_o = json.loads(o["payload"])
        bstat = W.batch_status(con, o["order_id"])
        batches_pending.append({
            "order_id": o["order_id"], "ref": o["ref"],
            "target": plan_o.get("target", o["qty_requested"]),
            **bstat,
        })

    return {
        "type": "state",
        "contract_version": C.CONTRACT_VERSION,
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
        "orders_pending": orders_pending,
        "batches_pending": batches_pending,
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
    """Log a standalone event not already covered by a backend/warehouse.py
    operation's own transaction (env, clock changes, dedup/validation
    outcomes, arrival fallback). db.log_event() no longer commits on its
    own, so this wraps it in its own one-statement transaction."""
    with DB.transaction(con):
        DB.log_event(con, clock.t_sim, kind, payload)


# ---------------------------------------------------------------------------
# Storing a box  (called by both the MQTT path and the L1 fallback)
# ---------------------------------------------------------------------------
# All the actual database work -- validation, slot assignment, the
# transaction, the event -- lives in backend/warehouse.py::create_box. This
# wrapper only updates the runtime STATE (crane cue, banner) that the rest
# of main.py's HMI plumbing reads, exactly once, after the commit succeeds.

def store_box(barcode_id: str, gross_g: float, source: str,
             t_c: float | None = None, rh: float | None = None,
             fw: str | None = None, batch_id: str | None = None,
             vision: dict | None = None) -> dict:
    res = W.create_box(con, clock.t_sim, barcode_id, gross_g, source,
                       t_c, rh, fw, batch_id=batch_id, vision=vision)
    STATE["crane"] = {"cmd": "store", "box_id": res["box_id"],
                      "slot_id": res["slot"]["slot_id"] if res.get("slot") else None,
                      "seq": STATE["crane"]["seq"] + 1}
    STATE["banner"] = {
        "kind": "quarantine" if res["state"] == "QUARANTINE" else "ok",
        "text": (res.get("reason") or
                 "%s stocke en %s — %d noyaux, sechage %.1f h"
                 % (res["box_id"], res["slot"]["slot_id"] if res.get("slot") else "-",
                    res.get("quantity", 0), E.required_cure_h())),
        "t_sim": clock.t_sim,
    }
    return res


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def loop_clock() -> None:
    """Advance simulated time; let boxes cure and reservation locks expire.

    Wrapped in try/except: a bad row or a transient DB error here must
    never kill this task silently (finding F2) -- curing and lock expiry
    would simply stop happening for the rest of the demo with no visible
    sign until someone notices boxes never turning READY.
    """
    tick_n = 0
    while True:
        try:
            now = clock.tick()
            cured = W.sweep_cured(con, now)
            if cured:
                # Criterion 4 names this moment explicitly ("passage du
                # seuil 24 h") -- it must be visible on the dashboard the
                # instant it happens, not just inferable from the state
                # column on the next poll.
                STATE["banner"] = {
                    "kind": "ok",
                    "text": "%s pret (24 h de sechage atteintes)"
                           % (cured[0] if len(cured) == 1
                              else "%d box" % len(cured)),
                    "t_sim": now}
            expired = W.sweep_expired(con, now)
            for e in expired:
                if STATE["last_order"] and STATE["last_order"].get("order_id") == e["order_id"]:
                    STATE["last_order"]["status"] = "CANCELLED"
                STATE["banner"] = {
                    "kind": "quarantine",
                    "text": "%s expiree: reservation non confirmee a temps"
                           % e["order_id"], "t_sim": now}
            shipped = W.auto_ship_ready_batches(con, now)
            for s in shipped:
                STATE["banner"] = {
                    "kind": "ok",
                    "text": "%s expedie automatiquement: %d noyaux"
                           % (s["order_id"], s.get("qty_allocated", 0)),
                    "t_sim": now}
            tick_n += 1
            if cured or expired or shipped or tick_n % 25 == 0:   # ~every 5 real seconds
                W.checkpoint_clock(con, now, clock.speed)
        except Exception as exc:                        # pragma: no cover
            print("[loop_clock] error (continuing): %s" % exc)
        await asyncio.sleep(0.2)


async def loop_ws() -> None:
    while True:
        try:
            await broadcast()
        except Exception as exc:                        # pragma: no cover
            print("[loop_ws] error (continuing): %s" % exc)
        await asyncio.sleep(1.0 / C.WS_HZ)


def _validate_box_done(payload: dict) -> str | None:
    """Return an error string, or None if the payload is well-formed enough
    to attempt dedup + creation. Anything that fails this must not reach
    warehouse.create_box -- and must not raise inside loop_mqtt_in either
    (finding F2: a malformed message on the public broker, from another
    team's device or a typo in a hand-crafted test message, must not stop
    telemetry/curing/WS for the rest of the session)."""
    if not isinstance(payload, dict):
        return "payload is not a JSON object"
    # Still named "ref" on the wire (firmware-compatibility: the board only
    # ever echoes this string back, never parses it) -- its content is the
    # scanned barcode_id since identification moved off the board (contract
    # 1.5). count_beam may still be present (older/manual boards send it)
    # but is no longer required or trusted for anything.
    barcode_id = payload.get("ref")
    if not isinstance(barcode_id, str) or not (0 < len(barcode_id) <= 24):
        return "ref (barcode_id) must be a non-empty string <= 24 chars"
    gg = payload.get("gross_g", 0)
    if isinstance(gg, bool) or not isinstance(gg, (int, float)) or not (0 <= gg <= 30000):
        return "gross_g must be a number in [0, 30000]"
    return None


async def handle_box_done(payload: dict) -> None:
    """One physical box_done -> at most one database box (contract 1.2
    §1.3). See algo.engine.dedup_verdict for the pure decision core; this
    function owns the runtime arrival window and the monotonic clock that
    feed it.
    """
    global _last_unsolicited
    err = _validate_box_done(payload)
    if err:
        event("box_done_invalid", {"error": err,
                                   "raw_keys": list(payload.keys())
                                   if isinstance(payload, dict) else None})
        return

    barcode_id = payload["ref"]
    gross_g = float(payload.get("gross_g", 0.0))
    fw = payload.get("fw")
    t_c = payload.get("t_c")
    rh = payload.get("rh")
    fp = E.box_fingerprint(barcode_id, gross_g, fw)
    now_mono = time.monotonic()

    STATE["device"]["last_seen_sim"] = clock.t_sim
    STATE["device"]["last_seen_mono"] = now_mono

    verdict = E.dedup_verdict(_arrival_window, fp, barcode_id, now_mono,
                              _last_unsolicited, C.ARRIVAL_GRACE_S,
                              C.UNSOLICITED_DEDUP_S)
    if verdict["action"] == "ignore":
        event("box_done_ignored", {"barcode_id": barcode_id,
                                   "gross_g": round(gross_g, 1),
                                   "reason": verdict["reason"]})
        print("[box_done] ignored duplicate: %s (%s)" % (barcode_id, verdict["reason"]))
        return

    if _arrival_window is None:
        _last_unsolicited = (fp, now_mono)

    batch_id = _arrival_window.get("batch_id") if _arrival_window else None
    vision = _arrival_window.get("vision") if _arrival_window else None
    res = store_box(barcode_id, gross_g, source="esp32", t_c=t_c, rh=rh, fw=fw,
                    batch_id=batch_id, vision=vision)

    if verdict["action"] == "resolve_window" and _arrival_window is not None:
        _arrival_window["status"] = "RESOLVED_L0"
        _arrival_window["resolved_at"] = now_mono
        _arrival_window["box_id"] = res["box_id"]

    await broadcast()
    print("[box_done] %s -> %s" % (barcode_id, res["state"]))


async def loop_mqtt_in() -> None:
    while True:
        topic, payload = await mq.inbox.get()
        try:
            if not isinstance(payload, dict):
                continue
            if topic == C.T_TELEMETRY:
                STATE["device"].update({
                    "state": payload.get("state", "IDLE"),
                    "count_beam": payload.get("count_beam", 0),
                    "gross_g": payload.get("gross_g", 0.0),
                    "stable": payload.get("stable", False),
                    "last_seen_sim": clock.t_sim,
                    "last_seen_mono": time.monotonic(),
                    "source": payload.get("src", "esp32"),
                })
                if "t_c" in payload and payload["t_c"] is not None:
                    STATE["env"] = {"t_c": payload["t_c"], "rh": payload["rh"]}
            elif topic == C.T_BOX_DONE:
                await handle_box_done(payload)
        except Exception as exc:                        # pragma: no cover
            # One bad/foreign message on the shared public broker must never
            # take telemetry or box arrivals down for the rest of the demo.
            print("[loop_mqtt_in] error handling %s (continuing): %s" % (topic, exc))


def _auto_register_barcode(ref: str, qty: int) -> str | None:
    """Convenience path for a quick test/demo run that just wants "a box of
    NY-114" without a separate registration step: mint a throwaway barcode
    for that reference, at the article's own unit_mass_g, and register it
    right now. Returns None if `ref` doesn't exist.

    The REAL workflow (a worker registers a barcode well before the box
    ever arrives, POST /api/barcodes) is unaffected and still fully
    supported -- this only fires when the caller passes `ref` instead of an
    already-registered `barcode_id`.
    """
    art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
    if art is None:
        return None
    barcode_id = "AUTO-%s-%s" % (ref, secrets.token_hex(3).upper())
    W.register_barcode(con, clock.t_sim, barcode_id, ref, art["unit_mass_g"])
    return barcode_id


async def run_arrival(barcode_id: str, qty: int, anomaly: str = "none",
                      batch_id: str | None = None) -> dict:
    """Play one box arrival through the plant model, at 10 Hz.

    L0: the ESP32 is listening, weighs, and answers on scw/<S>/dev/box_done.
    L1: no answer within the timeout -> the backend does the ESP32's
        arithmetic itself and stores the box anyway. Identical screen, demo
        never dies.

    `barcode_id` must already be registered (POST /api/barcodes) and unused
    -- that lookup already happened one level up in api_arrival, which is
    also where the convenience "just give me a ref" path auto-registers one.

    `batch_id` tags the resulting box to an IN_PRODUCTION order (see
    backend/warehouse.py::reserve) -- carried on the arrival window so both
    the L0 (handle_box_done) and L1 paths below tag the same box the same
    way, whichever one actually creates it.

    Opens an arrival window (see algo.engine.dedup_verdict) so a device
    answer that lands AFTER the L1 grace period is recognised as the same
    physical box, not a second one (finding F3) -- and checks `_epoch`
    after every frame so a reset/scenario mid-arrival aborts cleanly instead
    of storing a box into a database that was just wiped (finding F6).
    """
    global _arrival_window
    async with _arrival_lock:
        bc = DB.one(con, "SELECT * FROM barcodes WHERE barcode_id=?", (barcode_id,))
        if bc is None:
            return {"error": "unknown or unregistered barcode_id: %s" % barcode_id}
        if bc["used_by_box"]:
            return {"error": "barcode already used: %s" % barcode_id}

        art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (bc["ref"],))
        all_articles = DB.rows(con, "SELECT * FROM articles")
        swap = (PLANT.pick_swap_article(art, all_articles)
               if anomaly == "mismatch" and art else None)
        frames = PLANT.build_arrival(
            bc["unit_mass_g"], qty, anomaly,
            swap_unit_mass_g=(swap["unit_mass_g"] if swap else None))
        # The simulated vision station's own reading (criterion 2), taken
        # once for this arrival -- independent of whichever path (L0/L1)
        # ends up resolving the weight, so it must agree with `swap` above:
        # the same physical wrong reference the scale also weighed.
        vision = PLANT.simulate_vision(art, qty, anomaly, all_articles) if art else None
        my_epoch = _epoch
        _arrival_window = {"barcode_id": barcode_id, "status": "OPEN",
                           "resolved_at": None, "box_id": None,
                           "batch_id": batch_id, "vision": vision}

        # Still the wire field name "ref" (firmware-compatibility: the board
        # only ever echoes this string back, never parses it) -- its content
        # is the scanned barcode_id, not an article reference.
        mq.pub(C.T_CMD, {"cmd": "start_box", "ref": barcode_id})

        for f in frames:
            if _epoch != my_epoch:
                return {"mode": "aborted", "reason": "reset during arrival"}
            f = dict(f, t_c=STATE["env"]["t_c"], rh=STATE["env"]["rh"],
                     t_sim=round(clock.t_sim, 1))
            mq.pub(C.T_RAW, f)
            STATE["device"]["source"] = "plant"
            await asyncio.sleep(1.0 / C.RAW_HZ)

        # give the board a moment to declare the box
        for _ in range(25):
            await asyncio.sleep(0.1)
            if _epoch != my_epoch:
                return {"mode": "aborted", "reason": "reset during arrival"}
            if _arrival_window["status"] != "OPEN":
                STATE["mode"] = "L0"
                return {"mode": "L0", "box_id": _arrival_window.get("box_id")}

        # --- L1 fallback: byte-identical outcome, computed here --------------
        STATE["mode"] = "L1"
        gross = PLANT.final_gross_g(frames)
        res = store_box(barcode_id, gross, source="L1",
                        t_c=STATE["env"]["t_c"], rh=STATE["env"]["rh"],
                        batch_id=batch_id, vision=vision)
        _arrival_window["status"] = "RESOLVED_L1"
        _arrival_window["resolved_at"] = time.monotonic()
        _arrival_window["box_id"] = res["box_id"]
        event("arrival_fallback", {"barcode_id": barcode_id, "qty": qty,
                                   "anomaly": anomaly, "box_id": res["box_id"],
                                   "reason": "no device answer within grace period"})
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
    from the DB at runtime, so nothing else needs to know this ran.

    `cure_floor_h` is no longer accepted from the request: contract 1.2
    fixes drying at C.CURE_H for every reference (see
    backend/db.py::add_article for why it would be ignored anyway).
    """
    try:
        art = W.register_article(
            con, clock.t_sim, ref=body.get("ref", ""), label=body.get("label", ""),
            unit_mass_g=float(body.get("unit_mass_g", 0) or 0),
            tolerance_g=(float(body["tolerance_g"])
                        if body.get("tolerance_g") not in (None, "") else None),
            color=body.get("color") or None)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, 400)
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
        try:
            speed = float(body["speed"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "speed must be a number"}, 400)
        if speed not in C.ALLOWED_SPEEDS:
            return JSONResponse(
                {"error": "speed must be one of %s" % (C.ALLOWED_SPEEDS,)}, 400)
        clock.tick()
        clock.speed = speed
        event("clock_speed", {"speed": speed})
    if "jump_h" in body:
        try:
            hours = float(body["jump_h"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "jump_h must be a number"}, 400)
        # Bounded and strictly positive (finding: unvalidated jump_h could
        # go negative, running the clock backwards -- contract §6.1's "no
        # wall clock" rule is only half the guarantee; sim time also must
        # never run backwards).
        if not (0 < hours <= C.MAX_JUMP_H):
            return JSONResponse(
                {"error": "jump_h must be in (0, %.0f]" % C.MAX_JUMP_H}, 400)
        clock.tick()
        clock.jump(hours)
        event("clock_jump", {"hours": hours})
    W.checkpoint_clock(con, clock.t_sim, clock.speed)
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
    """The demo's main button: a registered box lands on the conveyor,
    scanner first, then the scale.

    Pass a pre-registered `barcode_id` for the real workflow (a worker ran
    POST /api/barcodes ahead of time). The convenience `ref` form instead
    auto-mints and registers a throwaway barcode for that reference on the
    spot, so a quick demo/test press of A doesn't need a separate
    registration step first.

    `batch_order_id`, when given, tags the resulting box to that
    IN_PRODUCTION order (backend/warehouse.py::reserve) -- production
    against the make-to-order shortfall, not a fresh addition to general
    stock.
    """
    barcode_id = body.get("barcode_id")
    qty = int(body.get("qty", 37))
    anomaly = body.get("anomaly", "none")
    batch_id = body.get("batch_order_id")
    if batch_id:
        order = DB.one(con, "SELECT status FROM orders WHERE order_id=?", (batch_id,))
        if not order:
            return JSONResponse({"error": "unknown order: %s" % batch_id}, 404)
        if order["status"] != "IN_PRODUCTION":
            return JSONResponse(
                {"error": "order %s is not in production (status %s)"
                          % (batch_id, order["status"])}, 409)
    if not barcode_id:
        ref = body.get("ref") or DB.ARTICLES[0][0]
        barcode_id = _auto_register_barcode(ref, qty)
        if barcode_id is None:
            return JSONResponse({"error": "unknown ref: %s" % ref}, 400)
    res = await run_arrival(barcode_id, qty, anomaly, batch_id=batch_id)
    await broadcast()
    return res


@app.post("/api/sim/box")
async def api_sim_box(body: dict):
    """L1 shortcut: create a box with no plant model and no ESP32 at all.

    Same `barcode_id`-or-`ref` convenience, and the same `batch_order_id`
    tagging, as /api/sim/arrival.
    """
    barcode_id = body.get("barcode_id")
    qty = int(body.get("qty", 37))
    batch_id = body.get("batch_order_id")
    if batch_id:
        order = DB.one(con, "SELECT status FROM orders WHERE order_id=?", (batch_id,))
        if not order:
            return JSONResponse({"error": "unknown order: %s" % batch_id}, 404)
        if order["status"] != "IN_PRODUCTION":
            return JSONResponse(
                {"error": "order %s is not in production (status %s)"
                          % (batch_id, order["status"])}, 409)
    if not barcode_id:
        ref = body.get("ref") or DB.ARTICLES[0][0]
        barcode_id = _auto_register_barcode(ref, qty)
        if barcode_id is None:
            return JSONResponse({"error": "unknown ref: %s" % ref}, 400)
    bc = DB.one(con, "SELECT * FROM barcodes WHERE barcode_id=?", (barcode_id,))
    gross = C.TARE_G + qty * (bc["unit_mass_g"] if bc else 200.0)
    res = store_box(barcode_id, gross, source="manual",
                    t_c=STATE["env"]["t_c"], rh=STATE["env"]["rh"],
                    batch_id=batch_id)
    await broadcast()
    return {k: v for k, v in res.items() if k != "slot"}


@app.post("/api/barcodes")
async def api_barcodes_add(body: dict):
    """A worker's action, well before any conveyor run (CDC step 1): label a
    physical box and register what it holds. See
    backend/warehouse.py::register_barcode."""
    try:
        res = W.register_barcode(con, clock.t_sim, body.get("barcode_id"),
                                 body.get("ref"), body.get("unit_mass_g"))
    except W.OpError as e:
        return JSONResponse({"error": e.message}, e.code)
    await broadcast()
    return res


@app.get("/api/barcodes")
async def api_barcodes_list(unused: bool = False):
    sql = "SELECT * FROM barcodes"
    if unused:
        sql += " WHERE used_by_box IS NULL"
    sql += " ORDER BY registered_sim DESC"
    return DB.rows(con, sql)


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
    ref = body.get("ref")
    if not ref:
        return JSONResponse({"error": "ref is required"}, 400)
    try:
        qty = int(body["qty"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "qty is required (integer)"}, 400)
    if qty <= 0:
        return JSONResponse({"error": "qty must be > 0"}, 400)

    plan = W.reserve(con, clock.t_sim, ref, qty)
    STATE["last_order"] = plan
    if plan["picks"]:
        STATE["crane"] = {"cmd": "pick", "box_id": plan["picks"][0]["box_id"],
                          "slot_id": plan["picks"][0]["slot_id"],
                          "seq": STATE["crane"]["seq"] + 1}
    await broadcast()
    return plan


@app.post("/api/demand/confirm")
async def api_confirm(body: dict):
    oid = body.get("order_id")
    if not oid:
        return JSONResponse({"error": "order_id is required"}, 400)
    try:
        res = W.confirm(con, clock.t_sim, oid)
    except W.OpError as e:
        return JSONResponse({"error": e.message}, e.code)

    row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (oid,))
    plan = json.loads(row["payload"])
    STATE["last_order"] = plan
    if not res["already"]:
        STATE["banner"] = {"kind": "ok", "text": "%s servie: %d noyaux preleves"
                           % (oid, plan["qty_allocated"]), "t_sim": clock.t_sim}
    await broadcast()
    return res


@app.post("/api/demand/cancel")
async def api_cancel(body: dict):
    oid = body.get("order_id")
    if not oid:
        return JSONResponse({"error": "order_id is required"}, 400)
    try:
        res = W.cancel(con, clock.t_sim, oid)
    except W.OpError as e:
        return JSONResponse({"error": e.message}, e.code)
    if STATE["last_order"] and STATE["last_order"].get("order_id") == oid:
        STATE["last_order"] = None
    await broadcast()
    return res


@app.post("/api/box/{box_id}/archive")
async def api_box_archive(box_id: str):
    """Close out a QUARANTINE/EMPTY box for good (contract 1.7) -- quarantine
    used to be a dead end with no endpoint that ever touched it again."""
    try:
        res = W.archive_box(con, clock.t_sim, box_id)
    except W.OpError as e:
        return JSONResponse({"error": e.message}, e.code)
    await broadcast()
    return res


@app.post("/api/box/{box_id}/recount")
async def api_box_recount(body: dict, box_id: str):
    """Re-present a QUARANTINED box's evidence (a re-weigh, and optionally a
    re-scan under the vision station) instead of leaving it stuck forever
    (contract 1.7). Body: `{"gross_g": 9420.5}`, optionally
    `{"anomaly": "none"}` to re-run the plant model's vision simulation for
    a fresh camera reading (omit for a plain re-weigh with no new vision
    evidence)."""
    try:
        gross_g = float(body.get("gross_g", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "gross_g must be a number"}, 400)
    vision = None
    if "anomaly" in body:
        b = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (box_id,))
        bc = DB.one(con, "SELECT * FROM barcodes WHERE barcode_id=?",
                    (b["code"] if b else None,))
        art = DB.one(con, "SELECT * FROM articles WHERE ref=?",
                    (bc["ref"],)) if bc else None
        if art and bc:
            all_articles = DB.rows(con, "SELECT * FROM articles")
            # `qty` defaults to this box's OWN weight-derived estimate, not
            # 0 -- a caller that asks for a fresh vision reading without
            # naming a quantity must not accidentally hand the simulator
            # "0 cores", which would fabricate a huge, spurious count
            # mismatch against the real weight and quarantine an otherwise
            # good recount.
            qty = body.get("qty")
            if qty is None:
                qty = E.count_from_weight(gross_g, bc["unit_mass_g"])
            vision = PLANT.simulate_vision(art, int(qty),
                                           body.get("anomaly", "none"), all_articles)
    try:
        res = W.recount_box(con, clock.t_sim, box_id, gross_g, vision=vision)
    except W.OpError as e:
        return JSONResponse({"error": e.message}, e.code)
    await broadcast()
    return res


@app.get("/api/events")
async def api_events(limit: int = 200):
    rows = DB.rows(con, "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


@app.post("/api/reset")
async def api_reset(body: dict | None = None):
    """Wipe + reseed. `{"seed": false}` keeps whatever is currently in
    `articles` instead of resetting to the four demo references (contract
    §2 -- previously accepted and silently ignored)."""
    global _epoch, _arrival_window, _last_unsolicited
    keep_articles = bool(body) and body.get("seed") is False

    _epoch += 1                      # abort any in-flight run_arrival (F6)
    _arrival_window = None
    _last_unsolicited = None

    W.reset_all(con, keep_articles=keep_articles)
    mq.pub(C.T_CMD, {"cmd": "reset"})   # tell a half-counted board to discard its box

    clock.t_sim = C.CLOCK_START_SIM
    clock.speed = C.DEFAULT_SPEED
    STATE["env"] = {"t_c": 24.0, "rh": 52.0}
    STATE["last_order"] = None
    STATE["banner"] = {"kind": "ok", "text": "Systeme reinitialise", "t_sim": 0.0}
    STATE["crane"] = {"cmd": "idle", "box_id": None, "slot_id": None,
                      "seq": STATE["crane"]["seq"]}
    STATE["device"].update({"online": False, "state": "IDLE", "count_beam": 0,
                            "gross_g": 0.0, "stable": False,
                            "last_seen_sim": -1e9, "last_seen_mono": -1e9})
    STATE["mode"] = "L0"
    await broadcast()
    return {"ok": True}


@app.post("/api/scenario")
async def api_scenario(body: dict):
    """One button that fills the warehouse with a believable history.

    Press it before the jury arrives so the rack is not empty and FIFO has
    something to be right about. Fixed 24 h cure (contract 1.2): at 34
    simulated hours, the three boxes stored in the first 10 h are READY and
    the three stored after are still DRYING -- see
    backend/warehouse.py::_SCENARIO_PLAN.
    """
    global _epoch, _arrival_window, _last_unsolicited
    name = body.get("name", "demo")
    if name == "demo":
        _epoch += 1
        _arrival_window = None
        _last_unsolicited = None
        result = W.load_demo_scenario(con)
        clock.t_sim = result["t_sim"]
        clock.speed = result["speed"]
        STATE["env"] = {"t_c": 22.0, "rh": 45.0}
        STATE["last_order"] = None
        STATE["crane"] = {"cmd": "idle", "box_id": None, "slot_id": None,
                          "seq": STATE["crane"]["seq"]}
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

    # Restore the simulated clock from its last checkpoint (finding F8: a
    # restart used to silently rewind to CLOCK_START_SIM while every box
    # kept its old t_in_sim, making READY boxes look uncured and losing the
    # pending order from the HMI).
    clock.t_sim, clock.speed = W.restore_clock(con)
    row = DB.one(con, "SELECT * FROM orders WHERE status='PENDING' "
                      "ORDER BY created_sim DESC LIMIT 1")
    if row:
        STATE["last_order"] = json.loads(row["payload"])
    print("[clock] restored t_sim=%.1f speed=%.0f" % (clock.t_sim, clock.speed))

    loop = asyncio.get_running_loop()
    mq.start(loop)
    asyncio.create_task(loop_clock())
    asyncio.create_task(loop_ws())
    asyncio.create_task(loop_mqtt_in())
    print("[scw] dashboard on http://localhost:8000   session=%s   contract=%s"
         % (C.SESSION, C.CONTRACT_VERSION))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
