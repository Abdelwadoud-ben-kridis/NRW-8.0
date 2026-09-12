"""Run me:  python -m pytest algo/test_engine.py -q     (or: python algo/test_engine.py)

These tests are your insurance against a jury question you cannot answer live.
Every one of them maps to a scored criterion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algo import engine as E

H = 3600.0
ART = {"ref": "NY-114", "label": "Noyau culasse 114",
       "unit_mass_g": 206.0, "tolerance_g": 6.0, "box_capacity": 40}


def _box(bid, ref, t_in, state, qty=40, req=24.0, **kw):
    b = {"box_id": bid, "article_ref": ref, "t_in_sim": t_in, "state": state,
         "qty_available": qty, "qty_initial": qty, "required_cure_h": req,
         "slot_id": "F0-C1-L1", "locked_by": None, "reason": None}
    b.update(kw)
    return b


# --- criterion 4: cure -------------------------------------------------------

def test_cure_floor_is_never_violated():
    assert E.required_cure_h(35.0, 10.0) == 24.0     # hot and dry: still 24 h
    assert E.required_cure_h(22.0, 45.0) == 24.0     # reference conditions


def test_cure_extends_in_cold_humid_room():
    r = E.required_cure_h(16.0, 80.0)
    assert r > 24.0 and round(r, 1) == 38.2


def test_is_cured_boundary():
    assert not E.is_cured(0.0, 24.0, 24 * H - 1)
    assert E.is_cured(0.0, 24.0, 24 * H)


# --- criteria 2 + 3: identification and quantity -----------------------------

def test_perfect_agreement_high_confidence():
    gross = E.TARE_G + 37 * 206.0
    r = E.assess_box(ART, 37, gross)
    assert r["quantity"] == 37 and r["confidence"] == "HAUTE" and r["accepted"]


def test_off_by_one_accepted_but_cautious():
    gross = E.TARE_G + 36 * 206.0
    r = E.assess_box(ART, 37, gross)
    assert r["accepted"] and r["confidence"] == "MOYENNE" and r["quantity"] == 36


def test_delta_two_goes_to_quarantine():
    gross = E.TARE_G + 35 * 206.0
    r = E.assess_box(ART, 37, gross)
    assert not r["accepted"] and r["state"] == "QUARANTINE"
    # and it must be reported as a COUNTING fault, not a wrong reference
    assert "ecart de comptage" in r["reason"]


def test_mislabelled_box_is_caught_by_unit_mass():
    # 37 cores that each weigh 260 g -> this is not NY-114 (206 g)
    gross = E.TARE_G + 37 * 260.0
    r = E.assess_box(ART, 37, gross)
    assert r["state"] == "QUARANTINE" and "incompatible" in r["reason"]
    assert "multiple entier" in r["reason"]


def test_dead_barrier_is_caught():
    gross = E.TARE_G + 37 * 206.0
    r = E.assess_box(ART, 0, gross)
    assert r["state"] == "QUARANTINE"


# --- criteria 5 + 6: FIFO and automatic proposal ------------------------------

def test_fifo_picks_oldest_first():
    boxes = [_box("BOX-2", "NY-114", 9 * H, "READY"),
             _box("BOX-1", "NY-114", 3 * H, "READY")]
    r = E.fifo_allocate(boxes, "NY-114", 10, 100 * H)
    assert r["picks"][0]["box_id"] == "BOX-1"


def test_uncured_box_is_rejected_with_a_reason():
    boxes = [_box("BOX-1", "NY-114", 0.0, "DRYING")]
    r = E.fifo_allocate(boxes, "NY-114", 5, 10 * H)
    assert r["picks"] == [] and r["status"] == "IMPOSSIBLE"
    assert r["rejected"][0]["reason"] == "sechage insuffisant"
    assert "14.0 h" in r["rejected"][0]["detail"]


def test_multi_box_allocation_and_shortfall():
    boxes = [_box("BOX-1", "NY-114", 1 * H, "READY", qty=22),
             _box("BOX-2", "NY-114", 5 * H, "READY", qty=18),
             _box("BOX-3", "NY-114", 9 * H, "READY", qty=40)]
    r = E.fifo_allocate(boxes, "NY-114", 40, 100 * H)
    assert [p["take"] for p in r["picks"]] == [22, 18]
    assert r["shortfall"] == 0
    # the newest box must appear as rejected-for-FIFO, not silently dropped
    assert any(x["reason"] == "plus recent (FIFO)" for x in r["rejected"])


def test_shortfall_is_reported():
    boxes = [_box("BOX-1", "NY-114", 1 * H, "READY", qty=5)]
    r = E.fifo_allocate(boxes, "NY-114", 40, 100 * H)
    assert r["qty_allocated"] == 5 and r["shortfall"] == 35


def test_quarantined_box_never_allocated():
    boxes = [_box("BOX-1", "NY-114", 1 * H, "QUARANTINE", reason="ecart = 3"),
             _box("BOX-2", "NY-114", 5 * H, "READY")]
    r = E.fifo_allocate(boxes, "NY-114", 10, 100 * H)
    assert r["picks"][0]["box_id"] == "BOX-2"
    assert r["rejected"][0]["reason"] == "quarantaine"


def test_other_references_are_not_touched():
    boxes = [_box("BOX-1", "NY-999", 0.0, "READY"),
             _box("BOX-2", "NY-114", 5 * H, "READY")]
    r = E.fifo_allocate(boxes, "NY-114", 10, 100 * H)
    assert len(r["picks"]) == 1 and r["picks"][0]["box_id"] == "BOX-2"
    assert all(x["box_id"] != "BOX-1" for x in r["rejected"])


# --- state machine -----------------------------------------------------------

def test_partial_pick_keeps_t_in_sim():
    b = _box("BOX-1", "NY-114", 5 * H, "PICKING", qty=40)
    patch = E.apply_pick(b, 10)
    assert patch["state"] == "READY" and patch["qty_available"] == 30
    assert "t_in_sim" not in patch          # FIFO position preserved


def test_full_pick_empties_the_box():
    b = _box("BOX-1", "NY-114", 5 * H, "PICKING", qty=10)
    assert E.apply_pick(b, 10)["state"] == "EMPTY"


def test_lock_expires_back_to_ready():
    b = _box("BOX-1", "NY-114", 0.0, "RESERVED", locked_by="ORD-1",
             lock_expires_sim=10 * H)
    assert E.tick_box(b, 9 * H) is None
    assert E.tick_box(b, 11 * H)["state"] == "READY"


def test_drying_becomes_ready_on_its_own():
    b = _box("BOX-1", "NY-114", 0.0, "DRYING")
    assert E.tick_box(b, 23 * H) is None
    assert E.tick_box(b, 25 * H)["state"] == "READY"


def test_illegal_transition_is_refused():
    assert E.can_transition("DRYING", "READY")
    assert not E.can_transition("DRYING", "PICKING")


# --- slot policy -------------------------------------------------------------

def test_slot_policy_prefers_low_and_near():
    free = [{"slot_id": "F1-C7-L9", "face": 1, "col": 7, "level": 9},
            {"slot_id": "F0-C2-L1", "face": 0, "col": 2, "level": 1},
            {"slot_id": "F0-C5-L1", "face": 0, "col": 5, "level": 1}]
    assert E.choose_slot(free, "NY-114")["slot_id"] == "F0-C2-L1"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  ok   %s" % name)
            except AssertionError as e:
                fails += 1
                print("  FAIL %s  %s" % (name, e))
    print("\n%s" % ("ALL GREEN" if not fails else "%d FAILURE(S)" % fails))
    sys.exit(1 if fails else 0)
