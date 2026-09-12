"""
backend/consistency.py — read-only database consistency checker.

Answers, mechanically, the question the plan's audit had to answer by hand:
"can the DB currently be in an impossible state?" Every check is a SELECT;
nothing here ever writes. Shared by:

    GET /api/db/check     (backend/dbview.py, for the DB Explorer badge)
    python -m backend.consistency   (CLI, works with the server stopped)

Each check returns one row: id, severity (PASS/WARN/FAIL), a count, up to 20
offending ids, and a plain-language explanation a judge or a 3 a.m. teammate
can act on. The overall result is the worst severity across all checks.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config as C
from backend import db as DB

_MAX_OFFENDERS = 20


def _row(check_id: str, severity: str, message: str, offenders=None) -> dict:
    offenders = offenders or []
    return {
        "id": check_id, "severity": severity, "count": len(offenders),
        "offending": offenders[:_MAX_OFFENDERS], "message": message,
    }


def _worse(a: str, b: str) -> str:
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    return a if order[a] >= order[b] else b


def run_checks(con, now_sim: float | None = None) -> dict:
    """Run every check and return {"overall", "t_sim", "checks": [...]}."""
    checks: list[dict] = []

    if now_sim is None:
        r = DB.one(con, "SELECT MAX(t_sim) AS m FROM events")
        now_sim = (r["m"] if r and r["m"] is not None else 0.0)

    # --- DB1: SQLite-level integrity ----------------------------------------
    integrity = con.execute("PRAGMA integrity_check").fetchall()
    fk_errs = DB.rows(con, "PRAGMA foreign_key_check")
    if len(integrity) == 1 and integrity[0][0] == "ok" and not fk_errs:
        checks.append(_row("DB1", "PASS", "SQLite integrity + foreign keys ok"))
    else:
        msgs = [r[0] if not isinstance(r, dict) else str(r) for r in integrity if r[0] != "ok"]
        checks.append(_row("DB1", "FAIL",
                           "sqlite integrity_check / foreign_key_check failed: %s"
                           % (msgs or fk_errs)))

    # --- DB2: rack geometry matches config -----------------------------------
    n_slots = con.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
    if n_slots == C.SLOT_COUNT:
        checks.append(_row("DB2", "PASS", "slot count matches config geometry (%d)" % n_slots))
    else:
        checks.append(_row("DB2", "FAIL",
                           "slots table has %d rows, config geometry expects %d "
                           "(FACES=%d COLS=%d LEVELS=%d) -- reseed"
                           % (n_slots, C.SLOT_COUNT, C.FACES, C.COLS, C.LEVELS)))

    # --- S1/S2: box <-> slot back-reference agreement -----------------------
    mismatch = DB.rows(con, """
        SELECT b.box_id, b.slot_id, s.occupied_by FROM boxes b
        LEFT JOIN slots s ON s.slot_id = b.slot_id
        WHERE b.slot_id IS NOT NULL
          AND (s.slot_id IS NULL OR s.occupied_by IS NOT b.box_id)
    """)
    mismatch += DB.rows(con, """
        SELECT s.slot_id, s.occupied_by, b.slot_id AS box_slot FROM slots s
        LEFT JOIN boxes b ON b.box_id = s.occupied_by
        WHERE s.occupied_by IS NOT NULL
          AND (b.box_id IS NULL OR b.slot_id IS NOT s.slot_id)
    """)
    ids = [r.get("box_id") or r.get("slot_id") for r in mismatch]
    checks.append(_row("S1", "PASS" if not mismatch else "FAIL",
                       "boxes.slot_id <-> slots.occupied_by agree both ways"
                       if not mismatch else
                       "%d box/slot back-reference mismatches" % len(mismatch), ids))

    dupe_slot = DB.rows(con, "SELECT slot_id, COUNT(*) n FROM boxes "
                             "WHERE slot_id IS NOT NULL GROUP BY slot_id HAVING n>1")
    dupe_box = DB.rows(con, "SELECT occupied_by, COUNT(*) n FROM slots "
                            "WHERE occupied_by IS NOT NULL GROUP BY occupied_by HAVING n>1")
    dupes = dupe_slot + dupe_box
    checks.append(_row("S2", "PASS" if not dupes else "FAIL",
                       "no slot holds two boxes and no box occupies two slots"
                       if not dupes else "%d duplicate occupancy rows" % len(dupes),
                       [r.get("slot_id") or r.get("occupied_by") for r in dupes]))

    # --- S3: only DRYING/READY/RESERVED may hold a slot ---------------------
    bad = DB.rows(con, "SELECT box_id, state FROM boxes WHERE slot_id IS NOT NULL "
                       "AND state NOT IN ('DRYING','READY','RESERVED')")
    checks.append(_row("S3", "PASS" if not bad else "FAIL",
                       "slotted boxes are all DRYING/READY/RESERVED" if not bad else
                       "%d boxes hold a slot in an illegal state" % len(bad),
                       [r["box_id"] for r in bad]))

    # --- S4: slots.reserved_for matches the occupying box's locked_by -------
    bad = DB.rows(con, """
        SELECT s.slot_id FROM slots s JOIN boxes b ON b.slot_id = s.slot_id
        WHERE (s.reserved_for IS NOT b.locked_by)
    """)
    checks.append(_row("S4", "PASS" if not bad else "FAIL",
                       "slots.reserved_for matches the occupying box's locked_by"
                       if not bad else "%d slot/box lock mismatches" % len(bad),
                       [r["slot_id"] for r in bad]))

    boxes = DB.rows(con, "SELECT * FROM boxes")

    # --- B1: only persisted states ------------------------------------------
    bad = [b["box_id"] for b in boxes if b["state"] not in E_PERSISTED_STATES]
    checks.append(_row("B1", "PASS" if not bad else "FAIL",
                       "every box.state is a persisted state" if not bad else
                       "%d boxes have a non-persisted/unknown state" % len(bad), bad))

    # --- B2: quantity bounds --------------------------------------------------
    bad = [b["box_id"] for b in boxes
           if not (0 <= b["qty_available"] <= b["qty_initial"])]
    sev = "FAIL" if bad else "PASS"
    warn_cap = []
    if not bad:
        arts = {a["ref"]: a for a in DB.rows(con, "SELECT * FROM articles")}
        warn_cap = [b["box_id"] for b in boxes if b["article_ref"] in arts
                   and b["qty_initial"] > arts[b["article_ref"]]["box_capacity"]]
        if warn_cap:
            sev = "WARN"
    checks.append(_row("B2", sev,
                       "0 <= qty_available <= qty_initial for every box" if not bad else
                       "%d boxes have an impossible quantity" % len(bad),
                       bad or warn_cap))

    # --- B3/B4/B5: state-specific shape --------------------------------------
    bad = [b["box_id"] for b in boxes
           if b["state"] in ("DRYING", "READY", "RESERVED")
           and (not b["slot_id"] or b["qty_available"] <= 0)]
    checks.append(_row("B3", "PASS" if not bad else "FAIL",
                       "DRYING/READY/RESERVED boxes all have a slot and qty>0"
                       if not bad else "%d violate that" % len(bad), bad))

    bad = [b["box_id"] for b in boxes if b["state"] == "EMPTY"
           and (b["qty_available"] != 0 or b["slot_id"] or b["locked_by"])]
    checks.append(_row("B4", "PASS" if not bad else "FAIL",
                       "EMPTY boxes have qty 0, no slot, no lock" if not bad else
                       "%d EMPTY boxes are malformed" % len(bad), bad))

    bad = [b["box_id"] for b in boxes if b["state"] == "QUARANTINE"
           and (not b["reason"] or b["slot_id"] or b["locked_by"])]
    checks.append(_row("B5", "PASS" if not bad else "FAIL",
                       "QUARANTINE boxes have a reason, no slot, no lock"
                       if not bad else "%d QUARANTINE boxes are malformed" % len(bad), bad))

    # --- B6: NULL article_ref only in QUARANTINE -----------------------------
    bad = [b["box_id"] for b in boxes
           if b["article_ref"] is None and b["state"] != "QUARANTINE"]
    checks.append(_row("B6", "PASS" if not bad else "FAIL",
                       "article_ref is NULL only for QUARANTINE boxes" if not bad
                       else "%d non-quarantine boxes have no article" % len(bad), bad))

    # --- T1: fixed cure -------------------------------------------------------
    bad = [b["box_id"] for b in boxes
           if abs(b["required_cure_h"] - C.CURE_H) > 0.01
           or abs(b["ready_at_sim"] - (b["t_in_sim"] + C.CURE_H * 3600.0)) > 1.0]
    checks.append(_row("T1", "PASS" if not bad else "FAIL",
                       "every box requires exactly %.0f h, ready_at_sim consistent" % C.CURE_H
                       if not bad else "%d boxes deviate from the fixed cure rule" % len(bad),
                       bad))

    # --- T2: no timestamps from the future -----------------------------------
    bad = [b["box_id"] for b in boxes if b["t_in_sim"] > now_sim + 1.0]
    ev_future = con.execute("SELECT COUNT(*) FROM events WHERE t_sim > ?",
                            (now_sim + 1.0,)).fetchone()[0]
    sev = "FAIL" if (bad or ev_future) else "PASS"
    checks.append(_row("T2", sev,
                       "no box or event timestamp is ahead of the current clock"
                       if sev == "PASS" else
                       "%d boxes and %d events are stamped in the future"
                       % (len(bad), ev_future), bad))

    # --- T3: READY boxes are actually past their cure time -------------------
    bad = [b["box_id"] for b in boxes if b["state"] == "READY" and now_sim < b["ready_at_sim"]]
    checks.append(_row("T3", "PASS" if not bad else "FAIL",
                       "every READY box has actually reached ready_at_sim"
                       if not bad else "%d READY boxes are not actually cured yet" % len(bad), bad))

    # --- T4: DRYING boxes overdue (the cure sweep should have caught them) --
    overdue = [(b["box_id"], now_sim - b["ready_at_sim"]) for b in boxes
              if b["state"] == "DRYING" and now_sim >= b["ready_at_sim"]]
    if not overdue:
        checks.append(_row("T4", "PASS", "no DRYING box is overdue for its cure sweep"))
    else:
        worst = max(d for _, d in overdue)
        sev = "FAIL" if worst > 60.0 else "WARN"
        checks.append(_row("T4", sev,
                           "%d DRYING boxes are overdue (worst: %.1fs past ready_at_sim)"
                           % (len(overdue), worst), [b for b, _ in overdue]))

    # --- L1/L2: lock shape -----------------------------------------------------
    orders_by_id = {o["order_id"]: o for o in DB.rows(con, "SELECT * FROM orders")}
    bad = []
    for b in boxes:
        if b["state"] == "RESERVED":
            ok = (b["locked_by"] and b["lock_expires_sim"] is not None
                 and b["locked_by"] in orders_by_id
                 and orders_by_id[b["locked_by"]]["status"] == "PENDING")
            if ok:
                picks = json.loads(orders_by_id[b["locked_by"]]["payload"]).get("picks", [])
                ok = any(p["box_id"] == b["box_id"] for p in picks)
            if not ok:
                bad.append(b["box_id"])
    checks.append(_row("L1", "PASS" if not bad else "FAIL",
                       "every RESERVED box is properly locked by a live PENDING order"
                       if not bad else "%d RESERVED boxes have a broken lock" % len(bad), bad))

    bad = [b["box_id"] for b in boxes if b["state"] != "RESERVED"
           and (b["locked_by"] is not None or b["lock_expires_sim"] is not None)]
    checks.append(_row("L2", "PASS" if not bad else "FAIL",
                       "non-RESERVED boxes carry no lock fields" if not bad else
                       "%d non-RESERVED boxes still have lock fields set" % len(bad), bad))

    # --- O1/O2/O3: order shape ------------------------------------------------
    orders = DB.rows(con, "SELECT * FROM orders")
    bad_status, bad_picks, bad_qty, bad_ref = [], [], [], []
    for o in orders:
        if o["status"] not in ("PENDING", "IMPOSSIBLE", "DONE", "CANCELLED"):
            bad_status.append(o["order_id"])
            continue
        plan = json.loads(o["payload"])
        if o["status"] == "IMPOSSIBLE" and plan.get("picks"):
            bad_picks.append(o["order_id"])
        allocated = sum(p["take"] for p in plan.get("picks", []))
        if allocated != o["qty_allocated"] or o["qty_allocated"] > o["qty_requested"]:
            bad_qty.append(o["order_id"])
    checks.append(_row("O1", "PASS" if not (bad_status or bad_picks or bad_qty) else "FAIL",
                       "order status/qty/picks are internally consistent"
                       if not (bad_status or bad_picks or bad_qty) else
                       "%d bad status, %d IMPOSSIBLE-with-picks, %d qty mismatches"
                       % (len(bad_status), len(bad_picks), len(bad_qty)),
                       bad_status + bad_picks + bad_qty))

    for o in orders:
        plan = json.loads(o["payload"])
        if o["status"] == "PENDING":
            for p in plan.get("picks", []):
                b = next((x for x in boxes if x["box_id"] == p["box_id"]), None)
                if not b or b["state"] != "RESERVED" or b["locked_by"] != o["order_id"]:
                    bad_ref.append(o["order_id"])
                    break
        else:
            for b in boxes:
                if b["locked_by"] == o["order_id"]:
                    bad_ref.append(o["order_id"])
                    break
    checks.append(_row("O2", "PASS" if not bad_ref else "FAIL",
                       "PENDING orders hold exactly their picks; closed orders hold none"
                       if not bad_ref else "%d orders disagree with their boxes" % len(bad_ref),
                       bad_ref))

    known_refs = {a["ref"] for a in DB.rows(con, "SELECT ref FROM articles")}
    unknown_ref = [o["order_id"] for o in orders if o["ref"] not in known_refs]
    checks.append(_row("O3", "PASS" if not unknown_ref else "WARN",
                       "every order references a known article" if not unknown_ref else
                       "%d orders reference an unknown article" % len(unknown_ref), unknown_ref))

    # --- E1/E2: event log shape -----------------------------------------------
    events = DB.rows(con, "SELECT * FROM events")
    bad_json = []
    for e in events:
        try:
            json.loads(e["payload"])
        except Exception:
            bad_json.append(e["id"])
    checks.append(_row("E1", "PASS" if not bad_json else "FAIL",
                       "every event payload parses as JSON" if not bad_json else
                       "%d events have unparseable payloads" % len(bad_json), bad_json))

    creation_kinds = {"box_in", "quarantine"}
    boxes_with_event = set()
    for e in events:
        if e["kind"] in creation_kinds:
            try:
                boxes_with_event.add(json.loads(e["payload"]).get("box_id"))
            except Exception:
                pass
    missing_creation = [b["box_id"] for b in boxes if b["box_id"] not in boxes_with_event]
    checks.append(_row("E2", "PASS" if not missing_creation else "WARN",
                       "every box has a creation event" if not missing_creation else
                       "%d boxes have no box_in/quarantine event" % len(missing_creation),
                       missing_creation))

    # --- A1: article sanity -----------------------------------------------------
    arts = DB.rows(con, "SELECT * FROM articles")
    bad_art = [a["ref"] for a in arts
              if a["unit_mass_g"] <= 0 or a["tolerance_g"] <= 0 or a["box_capacity"] <= 0]
    warn_cure = [a["ref"] for a in arts if abs(a["cure_floor_h"] - C.CURE_H) > 0.01]
    sev = "FAIL" if bad_art else ("WARN" if warn_cure else "PASS")
    checks.append(_row("A1", sev,
                       "articles have sane mass/tolerance/capacity" if not bad_art else
                       "%d articles have an invalid field" % len(bad_art),
                       bad_art or warn_cure))

    overall = "PASS"
    for c in checks:
        overall = _worse(overall, c["severity"])
    return {"overall": overall, "t_sim": now_sim, "checks": checks}


E_PERSISTED_STATES = ("DRYING", "READY", "RESERVED", "EMPTY", "QUARANTINE", "ARCHIVED")


if __name__ == "__main__":
    con = DB.connect()
    con.execute("PRAGMA query_only = ON")
    result = run_checks(con)
    print("Overall: %s   (t_sim=%.1f)" % (result["overall"], result["t_sim"]))
    for c in result["checks"]:
        mark = {"PASS": "  ok ", "WARN": " WARN", "FAIL": " FAIL"}[c["severity"]]
        print("%s %-4s %s" % (mark, c["id"], c["message"]))
        if c["offending"]:
            print("        -> " + ", ".join(str(x) for x in c["offending"]))
    sys.exit(0 if result["overall"] != "FAIL" else 1)
