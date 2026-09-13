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
       "unit_mass_g": 206.0, "tolerance_g": 6.0}
BARCODE = {"barcode_id": "BC-1", "ref": "NY-114", "unit_mass_g": 206.0}


def _box(bid, ref, t_in, state, qty=40, req=24.0, **kw):
    b = {"box_id": bid, "article_ref": ref, "t_in_sim": t_in, "state": state,
         "qty_available": qty, "qty_initial": qty, "required_cure_h": req,
         "slot_id": "F0-C1-L1", "locked_by": None, "reason": None}
    b.update(kw)
    return b


# --- criterion 4: cure -------------------------------------------------------

def test_cure_is_fixed_24h_always():
    # CDC requirement: exactly 24 h, the same for every box. No adaptive
    # model -- contract 1.2 dropped that idea; 1.8 removed the climate sensor.
    assert E.required_cure_h() == 24.0
    assert E.CURE_FLOOR_H == 24.0


def test_is_cured_boundary():
    assert not E.is_cured(0.0, 24.0, 24 * H - 1)
    assert E.is_cured(0.0, 24.0, 24 * H)


# --- criteria 2 + 3: identification and quantity -----------------------------

def test_weight_matching_barcode_is_accepted_high_confidence():
    gross = E.TARE_G + 37 * 206.0
    r = E.assess_box(BARCODE, ART, gross)
    assert r["quantity"] == 37 and r["confidence"] == "HAUTE" and r["accepted"]


def test_weight_mismatch_is_caught_by_unit_mass():
    # The physical cores in this crate are NOT what its barcode promised --
    # 37 cores that each weigh 260 g is not a clean multiple of 206 g.
    gross = E.TARE_G + 37 * 260.0
    r = E.assess_box(BARCODE, ART, gross)
    assert r["state"] == "QUARANTINE" and "incompatible" in r["reason"]
    assert not r["accepted"] and r["quantity"] == 0


def test_empty_box_is_quarantined():
    # scale reads only the tare weight -- no cores at all
    gross = E.TARE_G
    r = E.assess_box(BARCODE, ART, gross)
    assert not r["accepted"] and r["state"] == "QUARANTINE"
    assert r["quantity"] == 0


def test_a_large_clean_count_is_still_accepted():
    # No declared capacity any more -- nobody knows how many cores are in a
    # box ahead of time, that's the whole reason the scale exists. A large
    # but clean count is accepted just like a small one.
    gross = E.TARE_G + 90 * 206.0
    r = E.assess_box(BARCODE, ART, gross)
    assert r["accepted"] and r["quantity"] == 90


def test_each_barcode_carries_its_own_unit_mass():
    # a slightly heavier batch, registered on ITS OWN barcode rather than
    # the shared article average -- must count clean against that value,
    # not articles.unit_mass_g.
    heavier = {"barcode_id": "BC-2", "ref": "NY-114", "unit_mass_g": 210.0}
    gross = E.TARE_G + 37 * 210.0
    r = E.assess_box(heavier, ART, gross)
    assert r["accepted"] and r["quantity"] == 37


# --- criterion 2: vision identification cross-check (contract 1.7) ----------

SHAPE_114 = {"ref": "NY-114", "len_mm": 120.0, "wid_mm": 85.0, "h_mm": 50.0, "holes": 2}
SHAPE_220 = {"ref": "NY-220", "len_mm": 150.0, "wid_mm": 110.0, "h_mm": 70.0, "holes": 3}
CATALOGUE = [SHAPE_114, SHAPE_220]


def test_identify_core_matches_the_true_shape():
    vision = {"len_mm": 121.0, "wid_mm": 84.0, "h_mm": 51.0, "holes": 2}
    r = E.identify_core(vision, CATALOGUE)
    assert r["ref"] == "NY-114" and r["confidence"] == "HAUTE"


def test_identify_core_holes_mismatch_lowers_confidence():
    # Same overall size as NY-114, but the wrong core-print count -- a
    # camera reads holes exactly, so a hole-count mismatch on an otherwise
    # perfect size match must not still read as a confident, clean match.
    vision = {"len_mm": 120.0, "wid_mm": 85.0, "h_mm": 50.0, "holes": 3}
    r = E.identify_core(vision, CATALOGUE)
    assert r["confidence"] == "NULLE"


def test_mismatch_is_quarantined_regardless_of_quantity():
    # Weight alone can coincidentally land on a clean multiple of the wrong
    # reference's mass for SOME quantities (finding: 1 in 4 slipped through
    # before vision existed) -- vision must catch every one of them, for
    # every quantity, because it doesn't depend on the weight arithmetic at
    # all.
    for qty in range(1, 81):
        gross = E.TARE_G + qty * 206.0     # a clean NY-114 weight
        r = E.assess_box(BARCODE, ART, gross, vision_ref="NY-220")
        assert not r["accepted"] and r["state"] == "QUARANTINE", qty
        assert "vision" in r["reason"]


def test_vision_agreement_rescues_a_noisy_but_honest_box():
    # A large, real box drifts past the tight zero-vision tolerance on
    # honest per-core variance alone (finding: 25-60% of good boxes at
    # realistic noise). With a vision count that agrees (camera one core
    # short -- ordinary occlusion), a residual inside the box-size noise
    # band is a confident, correct count, not a quarantine and not an
    # undercount (contract 1.10).
    noisy_gross = E.TARE_G + 40 * 206.0 + 60.0     # 60 g off: outside 0.12*unit
    r = E.assess_box(BARCODE, ART, noisy_gross, vision_ref="NY-114", vision_count=39)
    assert r["accepted"] and r["confidence"] == "HAUTE", r
    assert r["quantity"] == 40, r


def test_residual_beyond_the_noise_band_is_accepted_at_moyenne():
    # A small box has a small noise band -- 60 g off on 5 cores is beyond
    # what per-core variance explains, but vision agrees, so it is accepted
    # at MOYENNE rather than guessed HAUTE or quarantined.
    r = E.assess_box(BARCODE, ART, E.TARE_G + 5 * 206.0 + 60.0,
                     vision_ref="NY-114", vision_count=5)
    assert r["accepted"] and r["confidence"] == "MOYENNE" and r["quantity"] == 5, r


def test_occlusion_never_turns_into_an_undercount():
    # contract 1.10 finding: min(weight, vision) stored 8.8% of honest
    # 37-core boxes as 36, because the camera only ever misses cores. Full
    # plant pipeline, every reference, random quantities.
    import random as _random
    from backend import db as _db
    from backend import plant as _plant
    articles = [{"ref": r[0], "unit_mass_g": r[2], "tolerance_g": r[3],
                "len_mm": r[4], "wid_mm": r[5], "h_mm": r[6], "holes": r[7]}
               for r in _db.ARTICLES]
    _random.seed(4321)
    wrong = haute = n = 0
    for _ in range(1500):
        art = _random.choice(articles)
        qty = _random.randint(1, 80)
        bc = {"barcode_id": "BC-X", "ref": art["ref"], "unit_mass_g": art["unit_mass_g"]}
        gross = _plant.final_gross_g(_plant.build_arrival(art["unit_mass_g"], qty, "none"))
        vision = _plant.simulate_vision(art, qty, "none", articles)
        idv = E.identify_core(vision, articles)
        r = E.assess_box(bc, art, gross,
                         vision_ref=idv["ref"] if idv["confidence"] != "NULLE" else None,
                         vision_count=vision["count_visible"],
                         vision_confidence=idv["confidence"])
        if r["accepted"]:
            n += 1
            wrong += r["quantity"] != qty
            haute += r["confidence"] == "HAUTE"
    # The residual ~1% is pure weighing physics, not occlusion: 80 light
    # NY-075 cores (88.5 g, 3% CV) spread +/-24 g against a 44 g rounding
    # half-width, so the scale alone occasionally lands one core off. The
    # old min(weight, vision) rule was ~9-10% wrong on the same pipeline.
    assert wrong / n < 0.02, "%d/%d accepted with the wrong quantity" % (wrong, n)
    assert haute / n > 0.85, "only %d/%d HAUTE" % (haute, n)


def test_vision_and_weight_disagreeing_a_lot_is_quarantined():
    r = E.assess_box(BARCODE, ART, E.TARE_G + 37 * 206.0,
                     vision_ref="NY-114", vision_count=30)
    assert not r["accepted"] and r["state"] == "QUARANTINE"


def test_realistic_core_variance_rarely_quarantines_an_honest_box():
    # Regression guard for the finding that a fixed absolute tolerance with
    # no second sensor quarantined 25-60% of honest boxes under realistic
    # ~3% per-core mass variance. With the vision count as a second,
    # independent measurement, the false-quarantine rate on ordinary
    # arrivals must stay low (some MOYENNE-confidence undercounts by a core
    # or two are an accepted, disclosed trade-off -- an outright wrongful
    # quarantine is not).
    import random as _random
    from backend import db as _db
    from backend import plant as _plant
    articles = [{"ref": r[0], "unit_mass_g": r[2], "tolerance_g": r[3],
                "len_mm": r[4], "wid_mm": r[5], "h_mm": r[6], "holes": r[7]}
               for r in _db.ARTICLES]
    _random.seed(1234)
    quarantined = 0
    n = 800
    for _ in range(n):
        art = _random.choice(articles)
        qty = _random.randint(1, 80)
        bc = {"barcode_id": "BC-X", "ref": art["ref"], "unit_mass_g": art["unit_mass_g"]}
        frames = _plant.build_arrival(art["unit_mass_g"], qty, "none")
        gross = _plant.final_gross_g(frames)
        vision = _plant.simulate_vision(art, qty, "none", articles)
        idv = E.identify_core(vision, articles)
        vref = idv["ref"] if idv["confidence"] != "NULLE" else None
        r = E.assess_box(bc, art, gross, vision_ref=vref,
                         vision_count=vision.get("count_visible"))
        if not r["accepted"]:
            quarantined += 1
    assert quarantined / n < 0.02, "%d/%d honest boxes wrongly quarantined" % (quarantined, n)


def test_mismatch_anomaly_is_caught_end_to_end_for_every_quantity():
    # Full pipeline (plant model -> identify_core -> assess_box), not just
    # assess_box in isolation -- every quantity 1..80, every reference,
    # must be quarantined for a "mismatch" arrival (finding: weight alone
    # missed 1 case in 4 for specific quantities that happened to round
    # onto a clean multiple of the wrong reference's mass).
    from backend import db as _db
    from backend import plant as _plant
    articles = [{"ref": r[0], "unit_mass_g": r[2], "tolerance_g": r[3],
                "len_mm": r[4], "wid_mm": r[5], "h_mm": r[6], "holes": r[7]}
               for r in _db.ARTICLES]
    for art in articles:
        for qty in range(1, 81):
            bc = {"barcode_id": "BC-X", "ref": art["ref"], "unit_mass_g": art["unit_mass_g"]}
            swap = _plant.pick_swap_article(art, articles)
            frames = _plant.build_arrival(art["unit_mass_g"], qty, "mismatch",
                                          swap_unit_mass_g=swap["unit_mass_g"])
            gross = _plant.final_gross_g(frames)
            vision = _plant.simulate_vision(art, qty, "mismatch", articles)
            idv = E.identify_core(vision, articles)
            vref = idv["ref"] if idv["confidence"] != "NULLE" else None
            r = E.assess_box(bc, art, gross, vision_ref=vref,
                             vision_count=vision.get("count_visible"))
            assert not r["accepted"], (art["ref"], qty, r)


def test_quarantine_can_re_enter_drying_on_a_successful_recount():
    # contract 1.7: quarantine is no longer a dead end for a box whose
    # barcode is still known and valid.
    assert E.can_transition("QUARANTINE", "DRYING")
    assert E.can_transition("QUARANTINE", "ARCHIVED")
    assert not E.can_transition("QUARANTINE", "READY")


# --- criteria 5 + 6: FIFO and automatic proposal ------------------------------

def test_fifo_picks_oldest_first():
    boxes = [_box("BOX-2", "NY-114", 9 * H, "READY"),
             _box("BOX-1", "NY-114", 3 * H, "READY")]
    r = E.fifo_allocate(boxes, "NY-114", 10, 100 * H)
    assert r["picks"][0]["box_id"] == "BOX-1"


def test_fifo_tiebreak_is_numeric_not_lexicographic():
    # Same t_in_sim (speed 0, or a scenario load): BOX-2 must sort before
    # BOX-10 as a NUMBER, not as a string ("BOX-10" < "BOX-2" lexically).
    boxes = [_box("BOX-10", "NY-114", 5 * H, "READY", qty=5),
             _box("BOX-2", "NY-114", 5 * H, "READY", qty=5)]
    r = E.fifo_allocate(boxes, "NY-114", 5, 100 * H)
    assert r["picks"][0]["box_id"] == "BOX-2"
    assert sorted(boxes, key=E.fifo_key)[0]["box_id"] == "BOX-2"


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


def test_allocation_never_splits_a_box():
    # 25 needed; the oldest box has 22, the next has 18. Whole-box-only
    # (contract 1.9, reversing 1.7's partial picks): BOX-1 is taken whole,
    # and closing the remaining 3-core gap still takes the WHOLE of BOX-2
    # (18) rather than splitting it -- qty_allocated overshoots to 40.
    boxes = [_box("BOX-1", "NY-114", 1 * H, "READY", qty=22),
             _box("BOX-2", "NY-114", 5 * H, "READY", qty=18)]
    r = E.fifo_allocate(boxes, "NY-114", 25, 100 * H)
    assert [p["take"] for p in r["picks"]] == [22, 18]
    assert r["picks"][0]["partial"] is False
    assert r["picks"][1]["partial"] is False
    assert r["qty_allocated"] == 40 and r["shortfall"] == 0


def test_batch_tagged_boxes_are_not_general_stock():
    # A box already produced FOR another order's production batch must not
    # be stealable by a fresh demand (finding: it used to be, and the batch
    # then shipped short with no warning).
    boxes = [_box("BOX-1", "NY-114", 1 * H, "DRYING", batch_id="ORD-9"),
             _box("BOX-2", "NY-114", 5 * H, "READY")]
    r = E.fifo_allocate(boxes, "NY-114", 5, 100 * H)
    assert r["picks"][0]["box_id"] == "BOX-2"
    assert any(x["box_id"] == "BOX-1" and x["reason"] == "reserve au lot"
              for x in r["rejected"])


def test_impossible_allocation_reports_an_eta_when_the_pipeline_can_close_it():
    boxes = [_box("BOX-1", "NY-114", 0.0, "READY", qty=5),
             _box("BOX-2", "NY-114", 10 * H, "DRYING", qty=20, req=24.0)]
    r = E.fifo_allocate(boxes, "NY-114", 20, 12 * H)
    assert r["status"] == "IMPOSSIBLE"
    assert r["eta_sim"] == 10 * H + 24.0 * H


def test_impossible_allocation_has_no_eta_when_the_pipeline_cannot_close_it():
    boxes = [_box("BOX-1", "NY-114", 0.0, "READY", qty=5)]
    r = E.fifo_allocate(boxes, "NY-114", 40, 12 * H)
    assert r["status"] == "IMPOSSIBLE" and r["eta_sim"] is None


def test_insufficient_total_stock_refuses_the_whole_reservation():
    # Only 5 whole-box cores exist for this ref; asking for 40 must not
    # reserve those 5 and call it a partial success -- a box can't be split
    # to make up the other 35, so nothing is reserved at all.
    boxes = [_box("BOX-1", "NY-114", 1 * H, "READY", qty=5)]
    r = E.fifo_allocate(boxes, "NY-114", 40, 100 * H)
    assert r["picks"] == [] and r["status"] == "IMPOSSIBLE"
    assert r["qty_allocated"] == 0 and r["shortfall"] == 40
    assert r["rejected"][0]["reason"] == "stock insuffisant"


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
    # apply_pick itself still supports a partial take (used generically by
    # confirm()) -- fifo_allocate just never generates one any more, since
    # contract 1.9 reverted to whole-box-only picks.
    b = _box("BOX-1", "NY-114", 5 * H, "RESERVED", qty=40)
    patch = E.apply_pick(b, 10)
    assert patch["state"] == "READY" and patch["qty_available"] == 30
    assert "t_in_sim" not in patch          # FIFO position preserved


def test_full_pick_empties_the_box():
    b = _box("BOX-1", "NY-114", 5 * H, "RESERVED", qty=10)
    assert E.apply_pick(b, 10)["state"] == "EMPTY"


def test_apply_pick_refuses_take_over_available():
    b = _box("BOX-1", "NY-114", 5 * H, "RESERVED", qty=10)
    try:
        E.apply_pick(b, 11)
        assert False, "should have raised"
    except ValueError:
        pass
    try:
        E.apply_pick(b, 0)
        assert False, "should have raised"
    except ValueError:
        pass


def test_lock_expires_back_to_ready():
    b = _box("BOX-1", "NY-114", 0.0, "RESERVED", locked_by="ORD-1",
             lock_expires_sim=10 * H)
    assert E.tick_box(b, 9 * H) is None
    assert E.tick_box(b, 11 * H)["state"] == "READY"
    assert E.lock_expired(b, 9 * H) is False
    assert E.lock_expired(b, 11 * H) is True


def test_drying_becomes_ready_on_its_own():
    b = _box("BOX-1", "NY-114", 0.0, "DRYING")
    assert E.tick_box(b, 23 * H) is None
    assert E.tick_box(b, 25 * H)["state"] == "READY"


def test_illegal_transition_is_refused():
    assert E.can_transition("DRYING", "READY")
    assert not E.can_transition("DRYING", "PICKING")


def test_box_birth_states():
    assert E.can_transition(None, "DRYING")
    assert E.can_transition(None, "QUARANTINE")
    assert not E.can_transition(None, "READY")


def test_reserved_goes_directly_to_empty_or_ready():
    # PICKING is a real-world stage but never a persisted box.state value --
    # confirm() writes RESERVED -> EMPTY or RESERVED -> READY directly.
    assert E.can_transition("RESERVED", "EMPTY")
    assert E.can_transition("RESERVED", "READY")
    assert not E.can_transition("RESERVED", "PICKING")


def test_archived_is_terminal():
    assert not E.can_transition("ARCHIVED", "READY")
    assert not E.can_transition("ARCHIVED", "DRYING")


def test_order_transitions():
    assert E.can_transition_order(None, "PENDING")
    assert E.can_transition_order(None, "IMPOSSIBLE")
    assert E.can_transition_order("PENDING", "DONE")
    assert E.can_transition_order("PENDING", "CANCELLED")
    assert not E.can_transition_order("DONE", "CANCELLED")
    assert not E.can_transition_order("CANCELLED", "DONE")
    assert not E.can_transition_order("IMPOSSIBLE", "DONE")


# --- arrival dedup (idempotency) ---------------------------------------------

def test_dedup_open_window_resolves():
    window = {"barcode_id": "BC-1", "status": "OPEN", "resolved_at": None}
    fp = E.box_fingerprint("BC-1", 9420.5, "1.0")
    v = E.dedup_verdict(window, fp, "BC-1", now_mono=100.0,
                        last_unsolicited=None, grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "resolve_window"


def test_dedup_late_answer_after_l1_is_ignored():
    # L1 already fired and resolved the window 3 s ago; the real board
    # answers late with the same barcode -- must not create a second box.
    window = {"barcode_id": "BC-1", "status": "RESOLVED_L1", "resolved_at": 97.0}
    fp = E.box_fingerprint("BC-1", 9420.5, "1.0")
    v = E.dedup_verdict(window, fp, "BC-1", now_mono=100.0,
                        last_unsolicited=None, grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "ignore"


def test_dedup_late_answer_outside_grace_is_a_new_box():
    window = {"barcode_id": "BC-1", "status": "RESOLVED_L1", "resolved_at": 50.0}
    fp = E.box_fingerprint("BC-1", 9420.5, "1.0")
    v = E.dedup_verdict(window, fp, "BC-1", now_mono=100.0,
                        last_unsolicited=None, grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "create"


def test_dedup_unsolicited_repeat_is_ignored():
    fp = E.box_fingerprint("BC-1", 9420.5, "1.0")
    last = (fp, 98.0)
    v = E.dedup_verdict(None, fp, "BC-1", now_mono=100.0,
                        last_unsolicited=last, grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "ignore"


def test_dedup_unsolicited_different_box_is_created():
    fp1 = E.box_fingerprint("BC-1", 9420.5, "1.0")
    fp2 = E.box_fingerprint("BC-1", 10200.0, "1.0")
    v = E.dedup_verdict(None, fp2, "BC-1", now_mono=100.0,
                        last_unsolicited=(fp1, 99.0), grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "create"


def test_dedup_no_window_no_history_is_created():
    fp = E.box_fingerprint("BC-1", 9420.5, "1.0")
    v = E.dedup_verdict(None, fp, "BC-1", now_mono=100.0,
                        last_unsolicited=None, grace_s=15.0,
                        unsolicited_dedup_s=5.0)
    assert v["action"] == "create"


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
