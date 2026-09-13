"""
backend/warehouse.py — transactional warehouse operations.

Every backend action that changes more than one row (or a row plus an event)
goes through exactly one function here, and every one of those functions
wraps its work in `db.transaction()`. That is the whole fix for the
class of bug the plan calls out repeatedly: "box.slot_id updated but
slots.occupied_by not", a double-deducted confirm, a cancel that steals
another order's lock. None of that can happen if the write either fully
lands or is fully rolled back, and it cannot happen twice if a repeat is
recognised as a repeat instead of re-applied.

This module imports `algo.engine` (pure decisions) and `backend.db` (SQLite
mechanics) but knows nothing about FastAPI, MQTT or asyncio -- `backend/main.py`
is the only caller, and every function here can be exercised directly from a
test with a throwaway `SCW_DB` file (see tools/test_backend.py).

Naming convention: every function takes `con` and `now_sim` (the caller's
already-ticked SimClock.t_sim) explicitly. Nothing in here reads a clock.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algo import engine as E
from backend import config as C
from backend import db as DB


class OpError(Exception):
    """A refused operation: a precondition was not met. `code` is the HTTP
    status backend/main.py should answer with (400 bad input, 404 unknown
    id, 409 conflict with current state)."""
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Clock checkpoint (meta table) -- survives a backend restart
# ---------------------------------------------------------------------------

def checkpoint_clock(con, t_sim: float, speed: float) -> None:
    """Persist the simulated clock so a restart can resume it instead of
    silently rewinding to CLOCK_START_SIM while boxes keep their old
    timestamps (plan §13 / finding F8)."""
    with DB.transaction(con):
        DB.meta_set(con, "t_sim", t_sim)
        DB.meta_set(con, "speed", speed)


def restore_clock(con) -> tuple[float, float]:
    """Read the last checkpoint, or fall back to config defaults on a fresh
    database. Also guards against a checkpoint older than the newest
    persisted timestamp in the DB (defensive: a checkpoint write that lost a
    race with a seed should never rewind time backwards past real data)."""
    t_sim = DB.meta_get(con, "t_sim")
    speed = DB.meta_get(con, "speed")
    t_sim = float(t_sim) if t_sim is not None else C.CLOCK_START_SIM
    speed = float(speed) if speed is not None else C.DEFAULT_SPEED

    newest = DB.one(con, "SELECT MAX(x) AS m FROM ("
                        "SELECT MAX(t_in_sim) AS x FROM boxes "
                        "UNION SELECT MAX(t_sim) AS x FROM events)")
    if newest and newest["m"] is not None and newest["m"] > t_sim:
        t_sim = float(newest["m"])
    return t_sim, speed


# ---------------------------------------------------------------------------
# Box creation  (contract §1.3 -- the only thing that should ever INSERT a box)
# ---------------------------------------------------------------------------

def _occupy_a_slot(con, box_id: str) -> dict | None:
    """Pick the best free CURING-rack slot and CAS-occupy it. Retries a few
    times if the CAS loses a race (single-connection design makes that
    vanishingly rare, but the retry costs nothing and makes the guarantee
    real, not assumed). Never hands out a STORAGE slot -- a box is only ever
    born into the curing rack; it can move to STORAGE later via relocate()."""
    for _ in range(4):
        free = DB.rows(con, "SELECT * FROM slots WHERE occupied_by IS NULL "
                            "AND reserved_for IS NULL AND zone='CURING'")
        slot = E.choose_slot(free, "")
        if slot is None:
            return None
        n = DB.cas_update(con, "slots", "slot_id", slot["slot_id"],
                          expect={"occupied_by": None}, patch={"occupied_by": box_id})
        if n == 1:
            return slot
        # someone else took it between the read and the CAS -- try again
    return None


def _occupy_a_storage_slot(con, box_id: str) -> dict | None:
    """Same CAS-retry pattern as _occupy_a_slot, but from the flat STORAGE
    pool -- there is no rack position to rank, so the first free row wins."""
    for _ in range(4):
        free = DB.rows(con, "SELECT * FROM slots WHERE occupied_by IS NULL "
                            "AND reserved_for IS NULL AND zone='STORAGE'")
        if not free:
            return None
        slot = free[0]
        n = DB.cas_update(con, "slots", "slot_id", slot["slot_id"],
                          expect={"occupied_by": None}, patch={"occupied_by": box_id})
        if n == 1:
            return slot
    return None


def _insert_box_row(con, now_sim: float, box_id: str, article_ref: str | None,
                    qty: int, state: str, count_weight: int,
                    gross_g: float, confidence: str, reason: str | None,
                    slot: dict | None, code: str) -> None:
    req_h = E.required_cure_h()
    con.execute(
        "INSERT INTO boxes(box_id,article_ref,qty_initial,qty_available,slot_id,"
        "state,t_in_sim,required_cure_h,ready_at_sim,count_beam,count_weight,"
        "gross_g,confidence,reason,code) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
        (box_id, article_ref, qty, qty, slot["slot_id"] if slot else None, state,
         now_sim, req_h, now_sim + req_h * 3600.0, count_weight,
         gross_g, confidence, reason, code))


def create_box(con, now_sim: float, barcode_id: str, gross_g: float,
               source: str, t_c: float | None = None, rh: float | None = None,
               fw: str | None = None) -> dict:
    """Create exactly one box from one arrival's evidence.

    Called only after the caller (backend/main.py) has already decided this
    message should produce a box -- deduplication is main.py's job (it owns
    the monotonic clock and the arrival window; see algo.engine.dedup_verdict
    and docs/contracts.md CONTRACT VERSION 1.4 §1.3), not this function's.

    Identification is a lookup, not a guess: `barcode_id` was scanned off
    the physical crate by the conveyor's scanner, and a worker registered it
    (register_barcode) well before this box ever arrived. Two things
    quarantine the box before assess_box ever runs, because neither is a
    counting question:
      - the barcode was never registered ("code-barre inconnu")
      - the barcode was already consumed by an earlier box ("deja utilise")
    """
    with DB.transaction(con):
        bc = DB.one(con, "SELECT * FROM barcodes WHERE barcode_id=?", (barcode_id,))
        box_id = DB.next_id(con, "boxes", "box_id", "BOX")
        evidence = {"gross_g": round(float(gross_g), 1),
                    "t_c": t_c, "rh": rh, "fw": fw, "source": source}

        if bc is None:
            reason = "code-barre inconnu: %s" % barcode_id
            _insert_box_row(con, now_sim, box_id, None, 0, "QUARANTINE",
                            0, gross_g, "NULLE", reason, None, barcode_id)
            DB.log_event(con, now_sim, "quarantine", {
                "box_id": box_id, "barcode_id": barcode_id,
                "reason": "code-barre inconnu", **evidence})
            return {"box_id": box_id, "state": "QUARANTINE", "reason": reason,
                   "code": barcode_id}

        if bc["used_by_box"]:
            reason = "code-barre deja utilise par %s" % bc["used_by_box"]
            _insert_box_row(con, now_sim, box_id, bc["ref"], 0, "QUARANTINE",
                            0, gross_g, "NULLE", reason, None, barcode_id)
            DB.log_event(con, now_sim, "quarantine", {
                "box_id": box_id, "barcode_id": barcode_id, "ref": bc["ref"],
                "reason": "code-barre deja utilise", **evidence})
            return {"box_id": box_id, "state": "QUARANTINE", "reason": reason,
                   "code": barcode_id}

        art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (bc["ref"],))
        verdict = E.assess_box(bc, art, gross_g)
        slot = None
        if verdict["accepted"]:
            slot = _occupy_a_slot(con, box_id)

        state = "DRYING" if (verdict["accepted"] and slot) else verdict["state"]
        reason = verdict.get("reason")
        if verdict["accepted"] and not slot:
            state, reason = "QUARANTINE", "aucun emplacement libre"

        qty = verdict["quantity"] if state != "QUARANTINE" else 0
        _insert_box_row(con, now_sim, box_id, bc["ref"], qty, state,
                        verdict["count_weight"], gross_g,
                        verdict["confidence"], reason, slot, barcode_id)

        # Consumed the instant it's scanned, accepted or not -- a sticker
        # that already went through the conveyor once can never be replayed
        # onto a second physical box.
        DB.cas_update(con, "barcodes", "barcode_id", barcode_id,
                      expect={"used_by_box": None}, patch={"used_by_box": box_id})

        kind = "quarantine" if state == "QUARANTINE" else "box_in"
        DB.log_event(con, now_sim, kind, {
            "box_id": box_id, "ref": bc["ref"], "qty": qty, "barcode_id": barcode_id,
            "slot": slot["slot_id"] if slot else None, "state": state,
            "confidence": verdict["confidence"],
            "reason": reason, "required_cure_h": round(E.required_cure_h(), 1),
            **evidence})
        DB.meta_set(con, "t_sim", now_sim)

        # `state` and `reason` are the ones actually written to the row
        # (they can differ from verdict's, e.g. "aucun emplacement libre"
        # overrides an accepted verdict into QUARANTINE) -- they must win
        # over verdict's own "state"/"reason" keys, so they are applied
        # AFTER spreading verdict, not before.
        return {"box_id": box_id, "slot": slot, "code": barcode_id,
               **{k: v for k, v in verdict.items() if k not in ("state", "reason")},
               "state": state, "reason": reason}


# ---------------------------------------------------------------------------
# Relocate a cured box out of the curing rack into overflow storage
# (POST /api/box/{id}/relocate)
# ---------------------------------------------------------------------------

def relocate(con, now_sim: float, box_id: str) -> dict:
    """CDC's second outcome for a cured box: production hasn't asked for it,
    so it moves out of the curing rack into a separate storage pool, freeing
    its curing slot for a new arrival. The box keeps its state (READY), its
    cure record and its FIFO position (t_in_sim untouched) -- only its
    physical location changes, so it stays fully eligible for a future pick.
    """
    with DB.transaction(con):
        sweep_cured(con, now_sim)
        b = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (box_id,))
        if not b:
            raise OpError("unknown box: %s" % box_id, code=404)
        if b["state"] != "READY":
            raise OpError("box %s is %s, must be READY to relocate to storage"
                          % (box_id, b["state"]), code=409)

        cur_slot = DB.one(con, "SELECT * FROM slots WHERE slot_id=?", (b["slot_id"],)) \
            if b["slot_id"] else None
        if cur_slot and cur_slot["zone"] == "STORAGE":
            raise OpError("box %s is already in storage" % box_id, code=409)

        # Free the CURING slot BEFORE claiming a STORAGE one -- uq_slots_occupied
        # (one slot per box_id) rejects a box_id that would momentarily occupy
        # two slots at once. If claiming the new slot then fails, this release
        # rolls back with everything else in the transaction (db.transaction
        # catches every exception, OpError included), so the box never ends
        # up truly slotless.
        if b["slot_id"]:
            n = DB.cas_update(con, "slots", "slot_id", b["slot_id"],
                              expect={"occupied_by": box_id}, patch={"occupied_by": None})
            if n != 1:
                raise OpError("internal: box %s's slot changed mid-relocate" % box_id,
                              code=409)

        new_slot = _occupy_a_storage_slot(con, box_id)
        if new_slot is None:
            raise OpError("aucun emplacement de stockage libre", code=409)

        n = DB.cas_update(con, "boxes", "box_id", box_id,
                          expect={"state": "READY"},
                          patch={"slot_id": new_slot["slot_id"]})
        if n != 1:
            raise OpError("internal: box %s changed state mid-relocate" % box_id,
                          code=409)

        DB.log_event(con, now_sim, "box_relocated", {
            "box_id": box_id, "ref": b["article_ref"],
            "from_slot": b["slot_id"], "to_slot": new_slot["slot_id"]})
        DB.meta_set(con, "t_sim", now_sim)
        return {"box_id": box_id, "from_slot": b["slot_id"],
               "to_slot": new_slot["slot_id"]}


# ---------------------------------------------------------------------------
# Reservation  (POST /api/demand)
# ---------------------------------------------------------------------------

def reserve(con, now_sim: float, ref: str, qty: int) -> dict:
    """FIFO-allocate `qty` of `ref` and lock every picked box for this order.

    Guarantees the same physical quantity can never be promised to two
    orders: this runs inside one IMMEDIATE transaction (db.transaction), so
    no other operation can interleave between reading the candidate boxes
    and locking them, and every lock write is a compare-and-set that only
    succeeds if the box is still READY and unlocked.
    """
    with DB.transaction(con):
        # settle any box that crossed its 24 h floor since loop_clock's last
        # 0.2 s tick, so the CAS below (which expects state="READY") agrees
        # with what fifo_allocate just decided is pickable -- without this,
        # a demand called right after a clock jump could pick a box whose
        # state column still said DRYING and 409 on the CAS.
        sweep_cured(con, now_sim)
        order_id = DB.next_id(con, "orders", "order_id", "ORD")
        boxes = DB.rows(con, "SELECT * FROM boxes")
        plan = E.fifo_allocate(boxes, ref, int(qty), now_sim, order_id)

        expires = now_sim + C.LOCK_TTL_H * 3600.0
        for p in plan["picks"]:
            n = DB.cas_update(con, "boxes", "box_id", p["box_id"],
                              expect={"state": "READY", "locked_by": None},
                              patch={"state": "RESERVED", "locked_by": order_id,
                                     "lock_expires_sim": expires})
            if n != 1:
                raise OpError(
                    "internal: box %s was no longer available to reserve"
                    % p["box_id"], code=409)
            if p.get("slot_id"):
                DB.cas_update(con, "slots", "slot_id", p["slot_id"],
                              expect={"reserved_for": None},
                              patch={"reserved_for": order_id})
            p["lock_expires_sim"] = expires

        con.execute(
            "INSERT INTO orders(order_id,ref,qty_requested,qty_allocated,"
            "status,created_sim,payload) VALUES (?,?,?,?,?,?,?)",
            (order_id, ref, int(qty), plan["qty_allocated"], plan["status"],
             now_sim, json.dumps(plan, ensure_ascii=False)))
        DB.log_event(con, now_sim, "demand", {
            "order_id": order_id, "ref": ref, "qty": int(qty),
            "allocated": plan["qty_allocated"],
            "picks": [p["box_id"] for p in plan["picks"]],
            "rejected": len(plan["rejected"]),
            "lock_expires_sim": expires if plan["picks"] else None})
        DB.meta_set(con, "t_sim", now_sim)
        return plan


# ---------------------------------------------------------------------------
# Confirm / cancel / expiry -- shared release logic
# ---------------------------------------------------------------------------

def _load_order(con, order_id: str) -> dict:
    row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not row:
        raise OpError("unknown order: %s" % order_id, code=404)
    return row


def confirm(con, now_sim: float, order_id: str) -> dict:
    """Apply every pick of a PENDING order. Idempotent: confirming an
    already-DONE order is a no-op that returns ok, not a second deduction
    (finding F1) -- and confirming a CANCELLED/IMPOSSIBLE order is refused,
    not silently applied to whatever the boxes happen to be now.
    """
    with DB.transaction(con):
        row = _load_order(con, order_id)
        if row["status"] == "DONE":
            return {"ok": True, "already": True}
        if row["status"] != "PENDING":
            raise OpError("order %s is %s, cannot confirm"
                          % (order_id, row["status"]), code=409)

        plan = json.loads(row["payload"])
        detail = []
        for p in plan["picks"]:
            b = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (p["box_id"],))
            if not b or b["state"] != "RESERVED" or b["locked_by"] != order_id:
                raise OpError(
                    "internal: box %s is no longer held by %s"
                    % (p["box_id"], order_id), code=409)
            patch = E.apply_pick(b, p["take"])
            n = DB.cas_update(con, "boxes", "box_id", b["box_id"],
                              expect={"state": "RESERVED", "locked_by": order_id},
                              patch=patch)
            if n != 1:
                raise OpError("internal: box %s changed under us" % b["box_id"], 409)
            slot_released = False
            if b["slot_id"]:
                if patch["state"] == "EMPTY":
                    DB.cas_update(con, "slots", "slot_id", b["slot_id"],
                                  expect={"occupied_by": b["box_id"]},
                                  patch={"occupied_by": None, "reserved_for": None})
                    DB.update(con, "boxes", "box_id", b["box_id"], {"slot_id": None})
                    slot_released = True
                else:
                    DB.cas_update(con, "slots", "slot_id", b["slot_id"],
                                  expect={"reserved_for": order_id},
                                  patch={"reserved_for": None})
            detail.append({"box_id": b["box_id"], "take": p["take"],
                           "qty_before": b["qty_available"],
                           "qty_after": patch["qty_available"],
                           "final_state": patch["state"],
                           "slot_released": slot_released})

        plan["status"] = "DONE"
        con.execute("UPDATE orders SET status='DONE', payload=? WHERE order_id=?",
                    (json.dumps(plan, ensure_ascii=False), order_id))
        DB.log_event(con, now_sim, "pick_done", {
            "order_id": order_id, "qty": plan["qty_allocated"], "boxes": detail})
        DB.meta_set(con, "t_sim", now_sim)
        return {"ok": True, "already": False, "qty_allocated": plan["qty_allocated"]}


def _release_order(con, now_sim: float, order_id: str, row: dict,
                   event_kind: str) -> list[str]:
    """Shared release path for an explicit cancel and an automatic lock
    expiry -- both put the order into CANCELLED and release only the boxes
    still actually locked by it (finding F5: a stale cancel must not touch
    a box that moved on, e.g. was already confirmed by a since-corrected
    payload, or steal another order's lock on a shared slot)."""
    plan = json.loads(row["payload"])
    released = []
    for p in plan["picks"]:
        n = DB.cas_update(con, "boxes", "box_id", p["box_id"],
                          expect={"state": "RESERVED", "locked_by": order_id},
                          patch={"state": "READY", "locked_by": None,
                                 "lock_expires_sim": None})
        if n == 1:
            released.append(p["box_id"])
            if p.get("slot_id"):
                DB.cas_update(con, "slots", "slot_id", p["slot_id"],
                              expect={"reserved_for": order_id},
                              patch={"reserved_for": None})

    plan["status"] = "CANCELLED"
    con.execute("UPDATE orders SET status='CANCELLED', payload=? WHERE order_id=?",
                (json.dumps(plan, ensure_ascii=False), order_id))
    DB.log_event(con, now_sim, event_kind,
                {"order_id": order_id, "released": released})
    DB.meta_set(con, "t_sim", now_sim)
    return released


def cancel(con, now_sim: float, order_id: str) -> dict:
    """Idempotent cancel: cancelling an already-CANCELLED order is a no-op.
    Cancelling a DONE or IMPOSSIBLE order is refused (finding F5 -- it must
    never resurrect an already-emptied box)."""
    with DB.transaction(con):
        row = _load_order(con, order_id)
        if row["status"] == "CANCELLED":
            return {"ok": True, "already": True}
        if row["status"] != "PENDING":
            raise OpError("order %s is %s, cannot cancel"
                          % (order_id, row["status"]), code=409)
        released = _release_order(con, now_sim, order_id, row, "order_cancel")
        return {"ok": True, "already": False, "released": released}


def sweep_expired(con, now_sim: float) -> list[dict]:
    """Find every PENDING order whose lock has run out and cancel it
    automatically (finding F4/F8: previously only the box came back to
    READY while the order stayed PENDING and confirmable forever).

    Cheap no-op fast path: a plain read first, so loop_clock's 0.2 s tick
    does not open a write transaction when nothing has expired (which is
    almost always).
    """
    due_boxes = DB.rows(con, "SELECT DISTINCT locked_by FROM boxes "
                             "WHERE state='RESERVED' AND lock_expires_sim <= ?",
                        (now_sim,))
    if not due_boxes:
        return []
    expired = []
    with DB.transaction(con):
        for row in due_boxes:
            order_id = row["locked_by"]
            if not order_id:
                continue
            order = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
            if not order or order["status"] != "PENDING":
                continue
            released = _release_order(con, now_sim, order_id, order, "order_expired")
            expired.append({"order_id": order_id, "released": released})
    return expired


def sweep_cured(con, now_sim: float) -> list[dict]:
    """Move every DRYING box whose 24 h have elapsed to READY. Same cheap
    no-op fast path as sweep_expired."""
    candidates = DB.rows(con, "SELECT * FROM boxes WHERE state='DRYING' "
                              "AND ready_at_sim <= ?", (now_sim,))
    if not candidates:
        return []
    cured = []
    with DB.transaction(con):
        for b in candidates:
            n = DB.cas_update(con, "boxes", "box_id", b["box_id"],
                              expect={"state": "DRYING"}, patch={"state": "READY"})
            if n == 1:
                DB.log_event(con, now_sim, "cured", {
                    "box_id": b["box_id"], "ready_at_sim": b["ready_at_sim"],
                    "after_h": round(b["required_cure_h"], 1)})
                cured.append(b["box_id"])
        if cured:
            DB.meta_set(con, "t_sim", now_sim)
    return cured


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------

def register_article(con, now_sim: float, **kwargs) -> dict:
    art = DB.add_article(con, **kwargs)   # raises ValueError -> main.py -> 400
    with DB.transaction(con):
        DB.log_event(con, now_sim, "article_new", {
            "ref": art["ref"], "label": art["label"],
            "unit_mass_g": art["unit_mass_g"]})
    return art


def register_barcode(con, now_sim: float, barcode_id: str, ref: str,
                     unit_mass_g: float) -> dict:
    """A worker's action, well before any conveyor run: label a physical box
    with `barcode_id`, declaring which reference it holds and that specific
    box's own measured per-noyau weight (batches vary slightly even within
    one reference, which is why this isn't just `articles.unit_mass_g`).

    This is the ONLY place a box's identity/weight is ever declared by a
    human. From here on, the conveyor's scanner (backend/warehouse.py::
    create_box) reads `barcode_id` back and looks up exactly what was
    registered here -- nobody re-types or re-counts anything at arrival.
    """
    barcode_id = (barcode_id or "").strip()
    ref = (ref or "").strip()
    if not barcode_id or not ref:
        raise OpError("barcode_id et ref sont obligatoires", code=400)
    if unit_mass_g is None or unit_mass_g <= 0:
        raise OpError("masse par noyau doit etre > 0", code=400)
    with DB.transaction(con):
        if DB.one(con, "SELECT 1 FROM barcodes WHERE barcode_id=?", (barcode_id,)):
            raise OpError("code-barre deja enregistre: %s" % barcode_id, code=409)
        if not DB.one(con, "SELECT 1 FROM articles WHERE ref=?", (ref,)):
            raise OpError("reference inconnue: %s" % ref, code=400)
        con.execute(
            "INSERT INTO barcodes(barcode_id,ref,unit_mass_g,registered_sim) "
            "VALUES (?,?,?,?)", (barcode_id, ref, float(unit_mass_g), now_sim))
        DB.log_event(con, now_sim, "barcode_registered", {
            "barcode_id": barcode_id, "ref": ref, "unit_mass_g": float(unit_mass_g)})
        return {"barcode_id": barcode_id, "ref": ref, "unit_mass_g": float(unit_mass_g)}


# ---------------------------------------------------------------------------
# Reset / scenario
# ---------------------------------------------------------------------------

def reset_all(con, keep_articles: bool = False) -> None:
    """Wipe the dynamic tables (contract `POST /api/reset`). `keep_articles`
    implements the `{"seed": false}` body -- the contract's `seed` flag was
    previously accepted and ignored (finding: PARTIAL)."""
    with DB.transaction(con):
        DB.seed(con, keep_articles=keep_articles)
        DB.log_event(con, C.CLOCK_START_SIM, "system_reset",
                    {"keep_articles": keep_articles})


# The rehearsed demo scenario: six boxes staggered over 34 simulated hours,
# three cured by the time it finishes loading (see docs/demo-script.md).
_SCENARIO_PLAN = [
    ("NY-114", 40, 0.0), ("NY-220", 24, 3.0), ("NY-114", 28, 7.0),
    ("NY-075", 55, 11.0), ("NY-114", 40, 19.0), ("NY-330", 12, 22.0),
]
_SCENARIO_FINAL_SIM = 34.0 * 3600.0


def load_demo_scenario(con) -> dict:
    """Fresh seed, then insert the six-box history directly (not via
    create_box, which always means "now") with historical t_in_sim stamps,
    all in one transaction. Returns the final clock the caller should adopt.
    """
    with DB.transaction(con):
        DB.seed(con)
        for i, (ref, qty, at_h) in enumerate(_SCENARIO_PLAN, start=1):
            t_in = at_h * 3600.0
            art = DB.one(con, "SELECT * FROM articles WHERE ref=?", (ref,))
            box_id = DB.next_id(con, "boxes", "box_id", "BOX")
            barcode_id = "BC-DEMO-%d" % i
            gross = C.TARE_G + qty * art["unit_mass_g"]
            slot = _occupy_a_slot(con, box_id)
            con.execute(
                "INSERT INTO barcodes(barcode_id,ref,unit_mass_g,registered_sim,"
                "used_by_box) VALUES (?,?,?,?,?)",
                (barcode_id, ref, art["unit_mass_g"], t_in, box_id))
            _insert_box_row(con, t_in, box_id, ref, qty, "DRYING" if slot else "QUARANTINE",
                            qty, gross, "HAUTE",
                            None if slot else "aucun emplacement libre", slot,
                            barcode_id)
            DB.log_event(con, t_in, "box_in", {
                "box_id": box_id, "ref": ref, "qty": qty, "barcode_id": barcode_id,
                "slot": slot["slot_id"] if slot else None, "state": "DRYING",
                "confidence": "HAUTE", "source": "scenario",
                "required_cure_h": round(E.required_cure_h(), 1)})
        # advance straight to the demo instant: 3 of the 6 boxes are cured
        # by 34 h (24 h floor from t_in <= 10h), 3 are still drying
        for b in DB.rows(con, "SELECT * FROM boxes WHERE state='DRYING' "
                              "AND ready_at_sim <= ?", (_SCENARIO_FINAL_SIM,)):
            DB.cas_update(con, "boxes", "box_id", b["box_id"],
                          expect={"state": "DRYING"}, patch={"state": "READY"})
            DB.log_event(con, _SCENARIO_FINAL_SIM, "cured", {
                "box_id": b["box_id"], "ready_at_sim": b["ready_at_sim"],
                "after_h": round(b["required_cure_h"], 1)})
        DB.meta_set(con, "t_sim", _SCENARIO_FINAL_SIM)
        DB.meta_set(con, "speed", C.DEFAULT_SPEED)
        DB.log_event(con, _SCENARIO_FINAL_SIM, "scenario_loaded",
                    {"name": "demo", "t_sim": _SCENARIO_FINAL_SIM})
        return {"t_sim": _SCENARIO_FINAL_SIM, "speed": C.DEFAULT_SPEED}
