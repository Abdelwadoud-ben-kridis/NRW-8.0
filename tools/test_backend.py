"""
tools/test_backend.py — DB integration tests for backend/warehouse.py.

    python tools/test_backend.py

No server needed: every test opens its own throwaway SQLite file (deleted
after each test), calls into backend/warehouse.py directly, and runs the
consistency checker at the end to catch anything the specific assertions
missed. This is what actually exercises the transactional guarantees the
plan is built around (double-confirm, cancel-of-DONE, reservation expiry,
FIFO dedup at the DB layer, unknown-barcode quarantine, atomic reset) --
algo/test_engine.py only covers the pure decision functions in isolation.
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config as C
from backend import consistency as CHECK
from backend import db as DB
from backend import warehouse as W

H = 3600.0
fails = []
_paths: dict[int, str] = {}      # sqlite3.Connection has no __dict__ of its own
_bc_seq = itertools.count(1)


def fresh_con():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)          # sqlite3.connect creates it; we just want the name
    con = DB.connect(path)
    DB.init(con)
    DB.seed(con)
    _paths[id(con)] = path
    return con


def cleanup(con):
    path = _paths.pop(id(con), None)
    con.close()
    if path:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def _bc(con, now_sim=0.0, ref="NY-114", unit_mass_g=206.0):
    """Register a fresh, uniquely-named barcode (a worker's action, ahead of
    any arrival) and return its id -- test convenience so each create_box
    call below gets its own never-before-used barcode."""
    barcode_id = "BC-%d" % next(_bc_seq)
    W.register_barcode(con, now_sim, barcode_id, ref, unit_mass_g)
    return barcode_id


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def assert_pass(con, now_sim, label):
    result = CHECK.run_checks(con, now_sim)
    ok = result["overall"] != "FAIL"
    check("consistency PASS after %s" % label, ok,
         [c for c in result["checks"] if c["severity"] == "FAIL"] if not ok else "")


# ---------------------------------------------------------------------------
def test_create_box_slots_both_sides():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, _bc(con), C.TARE_G + 37 * 206.0, "test")
        check("box created DRYING", res["state"] == "DRYING", res)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (res["box_id"],))
        slot = DB.one(con, "SELECT * FROM slots WHERE slot_id=?", (row["slot_id"],))
        check("box.slot_id set", row["slot_id"] is not None)
        check("slot.occupied_by matches", slot["occupied_by"] == res["box_id"])
        assert_pass(con, 0.0, "create_box")
    finally:
        cleanup(con)


def test_unknown_barcode_quarantines_with_null_article():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, "BC-NEVER-REGISTERED", 2000.0, "test")
        check("unknown barcode quarantined", res["state"] == "QUARANTINE")
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (res["box_id"],))
        check("article_ref is NULL (not misfiled)", row["article_ref"] is None)
        check("does not pollute NY-114", not any(
            b["article_ref"] == "NY-114" for b in DB.rows(con, "SELECT * FROM boxes")))
        assert_pass(con, 0.0, "unknown-barcode quarantine")
    finally:
        cleanup(con)


def test_reused_barcode_is_quarantined():
    con = fresh_con()
    try:
        barcode_id = _bc(con)
        first = W.create_box(con, 0.0, barcode_id, C.TARE_G + 10 * 206.0, "test")
        check("first scan of the barcode accepted", first["state"] == "DRYING")
        second = W.create_box(con, 1.0, barcode_id, C.TARE_G + 10 * 206.0, "test")
        check("replaying the same barcode is quarantined",
             second["state"] == "QUARANTINE")
        check("first box is untouched",
             DB.one(con, "SELECT state FROM boxes WHERE box_id=?",
                   (first["box_id"],))["state"] == "DRYING")
        assert_pass(con, 1.0, "reused-barcode quarantine")
    finally:
        cleanup(con)


def test_double_confirm_deducts_once():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        oid = plan["order_id"]
        r1 = W.confirm(con, 100 * H, oid)
        r2 = W.confirm(con, 100 * H, oid)
        check("first confirm applies", r1["already"] is False)
        check("second confirm is a no-op", r2["already"] is True)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        # the whole box is taken (never split), so one confirm empties it;
        # the point of this test is that the SECOND confirm doesn't try to
        # deduct another 40 and drive qty_available negative
        check("qty deducted exactly once", row["qty_available"] == 0, row["qty_available"])
        assert_pass(con, 100 * H, "double confirm")
    finally:
        cleanup(con)


def test_confirm_cancelled_order_refused():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        oid = plan["order_id"]
        W.cancel(con, 100 * H, oid)
        try:
            W.confirm(con, 100 * H, oid)
            check("confirming a cancelled order raises", False)
        except W.OpError as e:
            check("confirming a cancelled order raises", e.code == 409)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box untouched by the refused confirm", row["qty_available"] == 40)
        assert_pass(con, 100 * H, "confirm-cancelled refusal")
    finally:
        cleanup(con)


def test_cancel_done_order_refused_and_box_stays_empty():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        oid = plan["order_id"]
        W.confirm(con, 100 * H, oid)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("full pick empties the box", row["state"] == "EMPTY" and not row["slot_id"])
        try:
            W.cancel(con, 100 * H, oid)
            check("cancelling a DONE order raises", False)
        except W.OpError as e:
            check("cancelling a DONE order raises", e.code == 409)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("EMPTY box was not resurrected", row["state"] == "EMPTY")
        assert_pass(con, 100 * H, "cancel-done refusal")
    finally:
        cleanup(con)


def test_reservation_expiry_cancels_order_and_releases_box():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        oid = plan["order_id"]
        past_expiry = 100 * H + C.LOCK_TTL_H * 3600.0 + 1.0
        expired = W.sweep_expired(con, past_expiry)
        check("order swept as expired", any(e["order_id"] == oid for e in expired))
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (oid,))
        check("order is CANCELLED", row["status"] == "CANCELLED")
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box back to READY", box["state"] == "READY")
        check("t_in_sim unchanged", box["t_in_sim"] == 0.0)
        try:
            W.confirm(con, past_expiry, oid)
            check("confirming an expired order raises", False)
        except W.OpError:
            check("confirming an expired order raises", True)
        assert_pass(con, past_expiry, "reservation expiry")
    finally:
        cleanup(con)


def test_whole_box_pick_overshoots_and_empties_the_box():
    # A box is never split (contract 1.3): reserving 10 out of a 40-core box
    # takes the WHOLE box, overshooting qty_requested, rather than leaving
    # 30 cores stranded in a half-picked crate.
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        check("rounds up to the whole box (40, not 10)", plan["qty_allocated"] == 40)
        W.confirm(con, 100 * H, plan["order_id"])
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box fully emptied, not split", box["state"] == "EMPTY")
        check("slot released", box["slot_id"] is None)
        assert_pass(con, 100 * H, "whole-box pick")
    finally:
        cleanup(con)


def test_insufficient_stock_reserves_nothing():
    # Only 5 whole-box cores exist; asking for 40 must refuse the whole
    # reservation instead of locking those 5 and reporting a shortfall.
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 5 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 40)
        check("order is IMPOSSIBLE", plan["status"] == "IMPOSSIBLE")
        check("nothing allocated", plan["qty_allocated"] == 0)
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box untouched, still READY", box["state"] == "READY")
        check("box not locked", box["locked_by"] is None)
        assert_pass(con, 100 * H, "insufficient stock")
    finally:
        cleanup(con)


def test_double_allocation_is_impossible():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 20 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan_a = W.reserve(con, 100 * H, "NY-114", 20)
        plan_b = W.reserve(con, 100 * H, "NY-114", 20)
        check("order A got the box", plan_a["qty_allocated"] == 20)
        check("order B got nothing (already reserved)", plan_b["qty_allocated"] == 0)
        check("order B's rejection names the reservation",
             any(r["reason"] == "reserve" for r in plan_b["rejected"]) or
             any("serv" in r["reason"] for r in plan_b["rejected"]))
        assert_pass(con, 100 * H, "double allocation attempt")
    finally:
        cleanup(con)


def test_reset_is_atomic_and_checks_pass():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        W.reset_all(con, keep_articles=False)
        check("boxes wiped", con.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] == 0)
        check("slots rebuilt", con.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
             == C.SLOT_COUNT + C.STORAGE_SLOTS)
        check("meta checkpoint reset",
             float(DB.meta_get(con, "t_sim")) == C.CLOCK_START_SIM)
        assert_pass(con, 0.0, "reset")
    finally:
        cleanup(con)


def test_reset_keep_articles():
    con = fresh_con()
    try:
        W.register_article(con, 0.0, ref="NY-999", label="Test ref", unit_mass_g=100.0)
        W.reset_all(con, keep_articles=True)
        refs = {a["ref"] for a in DB.rows(con, "SELECT ref FROM articles")}
        check("kept the extra reference", "NY-999" in refs)
        check("boxes still wiped", con.execute("SELECT COUNT(*) FROM boxes").fetchone()[0] == 0)
        assert_pass(con, 0.0, "reset keep_articles")
    finally:
        cleanup(con)


def test_reset_wipes_barcodes():
    con = fresh_con()
    try:
        _bc(con)
        W.reset_all(con, keep_articles=False)
        check("barcodes wiped",
             con.execute("SELECT COUNT(*) FROM barcodes").fetchone()[0] == 0)
    finally:
        cleanup(con)


def test_scenario_loads_and_checks_pass():
    con = fresh_con()
    try:
        result = W.load_demo_scenario(con)
        check("scenario final t_sim is 34h", result["t_sim"] == 34.0 * 3600.0)
        boxes = DB.rows(con, "SELECT * FROM boxes")
        check("six boxes", len(boxes) == 6, len(boxes))
        ready = sum(1 for b in boxes if b["state"] == "READY")
        check("three cured", ready == 3, ready)
        assert_pass(con, result["t_sim"], "scenario load")
    finally:
        cleanup(con)


def test_clock_checkpoint_restore():
    con = fresh_con()
    try:
        W.checkpoint_clock(con, 12345.6, 60.0)
        t_sim, speed = W.restore_clock(con)
        check("t_sim restored", t_sim == 12345.6)
        check("speed restored", speed == 60.0)
    finally:
        cleanup(con)


def test_apply_pick_invalid_take_rolls_back_confirm():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        # tamper with the payload to request more than is actually available
        import json as _json
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (plan["order_id"],))
        bad = _json.loads(row["payload"])
        bad["picks"][0]["take"] = 999
        con.execute("UPDATE orders SET payload=? WHERE order_id=?",
                   (_json.dumps(bad), plan["order_id"]))
        con.commit()
        try:
            W.confirm(con, 100 * H, plan["order_id"])
            check("confirm with a tampered take raises", False)
        except Exception:
            check("confirm with a tampered take raises", True)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box qty untouched by the rolled-back confirm", row["qty_available"] == 10)
        order_row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (plan["order_id"],))
        check("order still PENDING (not half-confirmed)", order_row["status"] == "PENDING")
    finally:
        cleanup(con)


def test_relocate_moves_ready_box_to_storage_and_frees_curing_slot():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        box_id = res["box_id"]
        curing_slot = res["slot"]["slot_id"]
        DB.update(con, "boxes", "box_id", box_id, {"state": "READY"})

        moved = W.relocate(con, 100 * H, box_id)
        check("relocate reports the storage destination",
             moved["to_slot"].startswith("STORAGE-"), moved)

        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (box_id,))
        check("box kept READY", row["state"] == "READY")
        check("box now sits in the storage slot", row["slot_id"] == moved["to_slot"])
        old_slot = DB.one(con, "SELECT * FROM slots WHERE slot_id=?", (curing_slot,))
        check("old curing slot freed", old_slot["occupied_by"] is None)
        new_slot = DB.one(con, "SELECT * FROM slots WHERE slot_id=?", (moved["to_slot"],))
        check("new storage slot occupied by this box", new_slot["occupied_by"] == box_id)
        check("new slot really is zone STORAGE", new_slot["zone"] == "STORAGE")
        assert_pass(con, 100 * H, "relocate")
    finally:
        cleanup(con)


def test_relocate_refuses_a_box_that_is_not_ready():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        try:
            W.relocate(con, 0.0, res["box_id"])
            check("relocating a DRYING box raises", False)
        except W.OpError:
            check("relocating a DRYING box raises", True)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (res["box_id"],))
        check("box untouched by the refused relocate", row["state"] == "DRYING")
    finally:
        cleanup(con)


def test_relocate_refuses_a_box_already_in_storage():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, _bc(con), C.TARE_G + 10 * 206.0, "test")
        DB.update(con, "boxes", "box_id", res["box_id"], {"state": "READY"})
        W.relocate(con, 100 * H, res["box_id"])
        try:
            W.relocate(con, 100 * H, res["box_id"])
            check("relocating an already-stored box raises", False)
        except W.OpError:
            check("relocating an already-stored box raises", True)
        assert_pass(con, 100 * H, "double relocate refusal")
    finally:
        cleanup(con)


def test_relocated_box_keeps_fifo_position_and_stays_pickable():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        W.relocate(con, 100 * H, "BOX-1")
        plan = W.reserve(con, 100 * H, "NY-114", 40)
        check("FIFO still picks the relocated box", plan["picks"]
             and plan["picks"][0]["box_id"] == "BOX-1", plan)
        assert_pass(con, 100 * H, "reserve after relocate")
    finally:
        cleanup(con)


def test_register_barcode_rejects_duplicate_and_unknown_ref():
    con = fresh_con()
    try:
        barcode_id = _bc(con)
        try:
            W.register_barcode(con, 0.0, barcode_id, "NY-114", 206.0)
            check("registering a duplicate barcode_id raises", False)
        except W.OpError as e:
            check("registering a duplicate barcode_id raises", e.code == 409)
        try:
            W.register_barcode(con, 0.0, "BC-NEW", "NOPE", 206.0)
            check("registering against an unknown ref raises", False)
        except W.OpError as e:
            check("registering against an unknown ref raises", e.code == 400)
    finally:
        cleanup(con)


def test_barcode_carries_its_own_unit_mass_not_the_articles_average():
    # a heavier batch, registered on its own barcode -- must count clean
    # against ITS weight, not the shared article's 206.0 g average.
    con = fresh_con()
    try:
        barcode_id = _bc(con, unit_mass_g=210.0)
        res = W.create_box(con, 0.0, barcode_id, C.TARE_G + 37 * 210.0, "test")
        check("counts clean against the barcode's own unit mass",
             res["state"] == "DRYING" and res["quantity"] == 37, res)
    finally:
        cleanup(con)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("-- %s" % name)
            fn()
    print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
    sys.exit(1 if fails else 0)
