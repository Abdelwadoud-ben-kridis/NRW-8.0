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
import json
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
        # Whole-box-only (contract 1.9): the demand for 10 still takes the
        # ENTIRE 40-core box, emptying it. The point of this test is that
        # the SECOND confirm doesn't try to empty/deduct it again.
        check("box emptied exactly once", row["state"] == "EMPTY" and row["qty_available"] == 0,
              row["qty_available"])
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
    # Whole-box-only (contract 1.9, reversing 1.7's partial picks):
    # reserving 10 out of a 40-core box still takes the ENTIRE box,
    # overshooting the request rather than splitting it.
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 10)
        check("allocates the whole box, overshooting the request", plan["qty_allocated"] == 40)
        check("not marked as a partial pick", plan["picks"][0]["partial"] is False)
        W.confirm(con, 100 * H, plan["order_id"])
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("box fully emptied", box["state"] == "EMPTY")
        check("0 cores left", box["qty_available"] == 0)
        check("slot released", box["slot_id"] is None)
        assert_pass(con, 100 * H, "whole-box pick")
    finally:
        cleanup(con)


def test_next_demand_moves_on_to_the_next_oldest_box():
    # BOX-1 (older) gets fully consumed by the first order (whole-box-only,
    # contract 1.9); a second demand must fall through to BOX-2, not try to
    # re-pick the now-EMPTY BOX-1.
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        W.confirm(con, 100 * H, W.reserve(con, 100 * H, "NY-114", 10)["order_id"])

        bc2 = _bc(con)
        W.create_box(con, 5 * H, bc2, C.TARE_G + 50 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-2", {"state": "READY"})

        plan = W.reserve(con, 100 * H, "NY-114", 20)
        check("moves on to the next-oldest box", plan["picks"][0]["box_id"] == "BOX-2")
        check("takes the whole of it (overshoot allowed)", plan["picks"][0]["take"] == 50)
        assert_pass(con, 100 * H, "next box FIFO order")
    finally:
        cleanup(con)


def test_insufficient_stock_opens_a_production_batch():
    # Only 5 whole-box cores exist; asking for 40 must not reserve those 5
    # nor refuse outright -- it opens a production batch for the whole 40,
    # leaving existing stock completely untouched and available to others.
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 5 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 40)
        check("order is IN_PRODUCTION", plan["status"] == "IN_PRODUCTION")
        check("nothing allocated yet", plan["qty_allocated"] == 0)
        check("target is the full request", plan["target"] == 40)
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id='BOX-1'")
        check("existing box untouched, still READY", box["state"] == "READY")
        check("existing box not locked", box["locked_by"] is None)
        assert_pass(con, 100 * H, "insufficient stock opens a batch")
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
             == C.SLOT_COUNT)
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
        check("scenario clock starts at x1 (contract 1.10)", result["speed"] == 1.0,
             result["speed"])
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


def test_batch_ships_once_every_box_has_cured():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        check("order opened as a batch", plan["status"] == "IN_PRODUCTION")
        order_id = plan["order_id"]

        b1 = W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 10 * 206.0, "test")
        b2 = W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 20 * 206.0, "test")
        check("both batch boxes accepted",
             b1["state"] == "DRYING" and b2["state"] == "DRYING", (b1, b2))
        check("batch boxes tagged to the order",
             DB.one(con, "SELECT batch_id FROM boxes WHERE box_id=?",
                   (b1["box_id"],))["batch_id"] == order_id)

        try:
            W.confirm(con, 1.0, order_id)
            check("shipping before curing is refused", False)
        except W.OpError as e:
            check("shipping before curing is refused", e.code == 409)

        past_cure = 30 * H
        res = W.confirm(con, past_cure, order_id)
        check("batch ships once cured", res["ok"] and res["qty_allocated"] == 30, res)
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        check("order is DONE", row["status"] == "DONE")
        for bid in (b1["box_id"], b2["box_id"]):
            box = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (bid,))
            check("box %s emptied and released its slot" % bid,
                 box["state"] == "EMPTY" and box["slot_id"] is None)
        assert_pass(con, past_cure, "batch shipped")
    finally:
        cleanup(con)


def test_batch_ships_short_when_a_box_is_quarantined():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        order_id = plan["order_id"]
        good = W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 20 * 206.0, "test")
        # a mismatched weight for this barcode -> quarantined, not DRYING
        bad_bc = _bc(con)
        bad = W.produce_for_batch(con, 0.0, order_id, bad_bc, C.TARE_G + 999.0, "test")
        check("good box drying, bad box quarantined",
             good["state"] == "DRYING" and bad["state"] == "QUARANTINE", (good, bad))

        res = W.confirm(con, 30 * H, order_id)
        check("batch ships short", res["ok"] and res["qty_allocated"] == 20 and res["short"], res)
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        check("order still DONE despite the shortfall", row["status"] == "DONE")
        assert_pass(con, 30 * H, "batch shipped short")
    finally:
        cleanup(con)


def test_batch_refuses_to_ship_with_nothing_produced():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        try:
            W.confirm(con, 0.0, plan["order_id"])
            check("shipping an empty batch raises", False)
        except W.OpError:
            check("shipping an empty batch raises", True)
    finally:
        cleanup(con)


def test_cancelling_a_batch_releases_its_boxes_to_general_stock():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        order_id = plan["order_id"]
        box = W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 30 * 206.0, "test")
        W.cancel(con, 0.0, order_id)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (box["box_id"],))
        check("box's batch_id cleared", row["batch_id"] is None)
        check("box otherwise untouched (still DRYING)", row["state"] == "DRYING")
        order_row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        check("order is CANCELLED", order_row["status"] == "CANCELLED")
        check("batch payload.status is CANCELLED too (contract 1.10)",
             json.loads(order_row["payload"])["status"] == "CANCELLED")

        DB.update(con, "boxes", "box_id", box["box_id"], {"state": "READY"})
        plan2 = W.reserve(con, 100 * H, "NY-114", 30)
        check("released box is picked by a later FIFO order",
             plan2["status"] == "PENDING" and
             any(p["box_id"] == box["box_id"] for p in plan2["picks"]), plan2)
        assert_pass(con, 100 * H, "batch cancelled and box reused")
    finally:
        cleanup(con)


def test_produce_for_batch_refuses_a_non_production_order():
    con = fresh_con()
    try:
        W.create_box(con, 0.0, _bc(con), C.TARE_G + 40 * 206.0, "test")
        DB.update(con, "boxes", "box_id", "BOX-1", {"state": "READY"})
        plan = W.reserve(con, 100 * H, "NY-114", 40)
        check("this order was fully covered by FIFO", plan["status"] == "PENDING")
        try:
            W.produce_for_batch(con, 100 * H, plan["order_id"], _bc(con),
                                C.TARE_G + 10 * 206.0, "test")
            check("producing against a PENDING order raises", False)
        except W.OpError as e:
            check("producing against a PENDING order raises", e.code == 409)
    finally:
        cleanup(con)


def test_batch_boxes_cannot_be_stolen_by_another_demand():
    # finding fixed by contract 1.7: a box produced for one order's
    # production batch used to be reachable by fifo_allocate for a totally
    # different demand, letting the batch ship short with no warning.
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        order_id = plan["order_id"]
        b1 = W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 30 * 206.0, "test")

        past_cure = 30 * H
        stolen = W.reserve(con, past_cure, "NY-114", 5)
        check("a fresh demand cannot pick the batch's box",
             not any(p["box_id"] == b1["box_id"] for p in stolen["picks"]))
        check("fresh demand instead opens its OWN batch",
             stolen["status"] == "IN_PRODUCTION", stolen)
        assert_pass(con, past_cure, "batch box not stolen")
    finally:
        cleanup(con)


def test_auto_ship_ready_batches_ships_without_a_manual_confirm():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        order_id = plan["order_id"]
        W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 30 * 206.0, "test")

        past_cure = 30 * H
        shipped = W.auto_ship_ready_batches(con, past_cure)
        check("auto-ship reports the batch", any(s["order_id"] == order_id for s in shipped))
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        check("order is DONE with no manual confirm", row["status"] == "DONE")
        assert_pass(con, past_cure, "auto-ship")
    finally:
        cleanup(con)


def test_auto_ship_is_a_no_op_while_still_curing():
    con = fresh_con()
    try:
        plan = W.reserve(con, 0.0, "NY-114", 30)
        order_id = plan["order_id"]
        W.produce_for_batch(con, 0.0, order_id, _bc(con), C.TARE_G + 30 * 206.0, "test")
        shipped = W.auto_ship_ready_batches(con, 1.0 * H)
        check("nothing shipped while still drying", shipped == [])
        row = DB.one(con, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        check("order still IN_PRODUCTION", row["status"] == "IN_PRODUCTION")
    finally:
        cleanup(con)


def test_archive_box_closes_out_a_quarantined_box():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, "no-such-barcode", C.TARE_G + 100.0, "test")
        check("unknown barcode quarantined", res["state"] == "QUARANTINE")
        out = W.archive_box(con, 1.0, res["box_id"])
        check("archived", out["state"] == "ARCHIVED")
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (res["box_id"],))
        check("state persisted as ARCHIVED", row["state"] == "ARCHIVED")
        try:
            W.archive_box(con, 2.0, res["box_id"])
            check("archiving an already-archived box raises", False)
        except W.OpError as e:
            check("archiving an already-archived box raises", e.code == 409)
        assert_pass(con, 2.0, "archive")
    finally:
        cleanup(con)


def test_recount_box_accepts_a_good_reweigh():
    # A box quarantined on a mismeasured weight can be re-weighed and
    # re-enter the normal cure cycle (contract 1.7) -- quarantine is no
    # longer a dead end for a box whose barcode is still known.
    con = fresh_con()
    try:
        bc = _bc(con)
        bad = W.create_box(con, 0.0, bc, C.TARE_G + 999.0, "test")
        check("bad weighing quarantined", bad["state"] == "QUARANTINE")
        out = W.recount_box(con, 1.0, bad["box_id"], C.TARE_G + 40 * 206.0)
        check("recount accepted", out["accepted"] is True, out)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (bad["box_id"],))
        check("box now DRYING with a real slot",
             row["state"] == "DRYING" and row["slot_id"] is not None)
        check("qty from the corrected weight", row["qty_available"] == 40)
        check("t_in_sim re-stamped to the recount time", row["t_in_sim"] == 1.0)
        assert_pass(con, 1.0, "recount accepted")
    finally:
        cleanup(con)


def test_recount_cannot_clear_a_vision_mismatch_by_weight_alone():
    # contract 1.10: vision saw NY-075 cores in a crate labelled NY-114. A
    # re-weigh with no fresh camera reading must NOT re-admit it, even when
    # the weight happens to land on a clean multiple of 206 g -- only a new
    # vision reading that agrees with the barcode can.
    con = fresh_con()
    try:
        bc = _bc(con)
        wrong_shape = {"len_mm": 65.0, "wid_mm": 45.0, "h_mm": 30.0, "holes": 1,
                       "count_visible": 16}
        clean_gross = C.TARE_G + 16 * 206.0
        bad = W.create_box(con, 0.0, bc, clean_gross, "test", vision=wrong_shape)
        check("vision mismatch quarantined", bad["state"] == "QUARANTINE", bad)
        out = W.recount_box(con, 1.0, bad["box_id"], clean_gross)
        check("weight-only recount refused", out["accepted"] is False, out)
        row = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (bad["box_id"],))
        check("still QUARANTINE", row["state"] == "QUARANTINE")
        check("arrival vision evidence kept", row["vision_ref"] == "NY-075", row["vision_ref"])
        right_shape = {"len_mm": 120.0, "wid_mm": 85.0, "h_mm": 50.0, "holes": 2,
                       "count_visible": 16}
        out2 = W.recount_box(con, 2.0, bad["box_id"], clean_gross, vision=right_shape)
        check("recount with an agreeing fresh vision reading accepted",
             out2["accepted"] is True, out2)
        assert_pass(con, 2.0, "vision-mismatch recount")
    finally:
        cleanup(con)


def test_recount_box_refuses_without_a_known_barcode():
    con = fresh_con()
    try:
        res = W.create_box(con, 0.0, "no-such-barcode", C.TARE_G + 100.0, "test")
        try:
            W.recount_box(con, 1.0, res["box_id"], C.TARE_G + 40 * 206.0)
            check("recount without a real barcode raises", False)
        except W.OpError as e:
            check("recount without a real barcode raises", e.code == 409)
    finally:
        cleanup(con)


def test_unknown_reference_demand_is_refused_not_a_batch():
    # contract 1.10: an unknown ref used to open an IN_PRODUCTION batch.
    con = fresh_con()
    try:
        try:
            W.reserve(con, 0.0, "NOPE-999", 5)
            check("unknown-ref demand raises", False)
        except W.OpError as e:
            check("unknown-ref demand raises a 400", e.code == 400, e.message)
        check("no order row written",
             con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0)
        assert_pass(con, 0.0, "unknown-ref demand")
    finally:
        cleanup(con)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("-- %s" % name)
            fn()
    print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
    sys.exit(1 if fails else 0)
