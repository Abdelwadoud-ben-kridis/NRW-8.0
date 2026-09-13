"""
backend/dbview.py — read-only introspection API for the "Database Explorer".

This is a VISUAL AUGMENT, not a new source of truth: every endpoint here is a
GET that only ever runs SELECT. Nothing in this file can create, edit, or
delete a row — that stays the job of the operational endpoints in main.py.

Mounted at /api/db/* by main.py; the page that consumes it is dashboard/db.html.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib
import json

from fastapi import APIRouter, HTTPException, Query

from backend import consistency as CHECK
from backend import db as DB

router = APIRouter()

# Whitelisted — table names are never taken from the request as raw SQL.
TABLES = ("articles", "boxes", "slots", "orders", "events", "barcodes")

# Hand-described schema: PRAGMA gives us columns/types/FKs accurately, but a
# jury (or you, at 3 a.m.) reads the RELATIONSHIP faster from a sentence than
# from a constraint name. Kept next to the live introspection below, not
# instead of it, so this file breaks loudly if the two ever disagree.
#
# Contract 1.2 note: `slots.occupied_by`/`reserved_for` and `boxes.locked_by`
# ARE now maintained on every reserve/confirm/cancel/expiry (see
# backend/warehouse.py) -- database-guide.md previously claimed this while
# the code did not do it (contradiction C1/C2 in the hardening plan); it is
# true again as of this contract version, so the claim below is restored.
RELATIONS = [
    {"from": "boxes.article_ref", "to": "articles.ref", "kind": "fk",
     "note": "every box holds cores of exactly one reference; NULL only "
             "while state=QUARANTINE and the declared reference was unknown"},
    {"from": "boxes.code", "to": "barcodes.barcode_id", "kind": "soft",
     "note": "the barcode a worker scanned off this physical crate (contract "
             "1.5); NULL only impossible in practice, but no FK-enforced row "
             "when the scan itself was of an unregistered code"},
    {"from": "barcodes.ref", "to": "articles.ref", "kind": "fk",
     "note": "a worker declares which reference a physical box holds when "
             "registering its barcode, well before it ever arrives"},
    {"from": "barcodes.used_by_box", "to": "boxes.box_id", "kind": "soft",
     "note": "set the instant the conveyor's scanner reads this barcode "
             "(backend/warehouse.py::create_box); a barcode can be consumed "
             "at most once, accepted or quarantined"},
    {"from": "boxes.slot_id", "to": "slots.slot_id", "kind": "fk",
     "note": "a box occupies at most one slot (NULL while incoming/quarantined); "
             "a partial unique index (uq_boxes_slot) enforces one box per slot"},
    {"from": "slots.occupied_by", "to": "boxes.box_id", "kind": "soft",
     "note": "back-reference kept in sync with boxes.slot_id in the same "
             "transaction, not FK-enforced; a partial unique index "
             "(uq_slots_occupied) enforces one slot per box"},
    {"from": "slots.reserved_for", "to": "orders.order_id", "kind": "soft",
     "note": "set while a pick is RESERVED; cleared on confirm/cancel/expiry "
             "(backend/warehouse.py), in the same transaction as the box"},
    {"from": "boxes.locked_by", "to": "orders.order_id", "kind": "soft",
     "note": "set together with slots.reserved_for; the pair is checked by "
             "consistency check S4"},
    {"from": "orders.payload", "to": "boxes.box_id", "kind": "json",
     "note": "the FIFO plan (picks[] / rejected[]) is stored as a JSON blob, "
             "not rows; payload.status is kept equal to the status column"},
    {"from": "events.payload", "to": "*", "kind": "json",
     "note": "append-only log; every mutation above also writes one event "
             "row in the SAME transaction (backend/db.py::log_event no "
             "longer commits on its own)"},
]


@contextlib.contextmanager
def _con():
    """A read-only connection that always closes, even if the endpoint
    raises partway through (previously every endpoint opened one with a
    bare `con.close()` at the end, which never ran on an exception)."""
    con = DB.connect()
    con.execute("PRAGMA query_only = ON")   # belt-and-braces: refuse writes at the driver level
    try:
        yield con
    finally:
        con.close()


def _check_table(name: str) -> str:
    if name not in TABLES:
        raise HTTPException(404, "unknown table: %s" % name)
    return name


@router.get("/schema")
def schema():
    with _con() as con:
        tables = []
        for t in TABLES:
            cols = DB.rows(con, "PRAGMA table_info(%s)" % t)
            fks = DB.rows(con, "PRAGMA foreign_key_list(%s)" % t)
            count = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            tables.append({
                "name": t,
                "row_count": count,
                "columns": [{"name": c["name"], "type": c["type"],
                            "pk": bool(c["pk"]), "notnull": bool(c["notnull"]),
                            "default": c["dflt_value"]} for c in cols],
                "foreign_keys": [{"column": f["from"], "ref_table": f["table"],
                                  "ref_column": f["to"]} for f in fks],
            })
        return {"tables": tables, "relations": RELATIONS}


@router.get("/tables")
def tables():
    with _con() as con:
        return [{"name": t, "row_count": con.execute(
            "SELECT COUNT(*) FROM %s" % t).fetchone()[0]} for t in TABLES]


@router.get("/table/{name}")
def table(name: str, limit: int = Query(50, ge=1, le=500),
          offset: int = Query(0, ge=0), q: str = Query("")):
    name = _check_table(name)
    with _con() as con:
        cols = [c["name"] for c in DB.rows(con, "PRAGMA table_info(%s)" % name)]

        where, args = "", []
        if q:
            text_cols = [c for c in cols if c not in
                        ("gross_g", "count_beam", "count_weight", "unit_mass_g",
                         "tolerance_g", "t_in_sim", "required_cure_h",
                         "ready_at_sim", "lock_expires_sim", "t_sim", "id")]
            if text_cols:
                where = "WHERE " + " OR ".join("%s LIKE ?" % c for c in text_cols)
                args = ["%%%s%%" % q] * len(text_cols)

        total = con.execute("SELECT COUNT(*) FROM %s %s" % (name, where), args).fetchone()[0]
        order = "id DESC" if name == "events" else "rowid DESC"
        rows = DB.rows(con, "SELECT * FROM %s %s ORDER BY %s LIMIT ? OFFSET ?"
                      % (name, where, order), args + [limit, offset])
        return {"table": name, "columns": cols, "rows": rows,
               "total": total, "limit": limit, "offset": offset}


@router.get("/stats")
def stats():
    with _con() as con:
        by_state = {r["state"]: r["n"] for r in DB.rows(
            con, "SELECT state, COUNT(*) n FROM boxes GROUP BY state")}

        by_ref = DB.rows(con, """
            SELECT a.ref, a.label, a.color,
                   COALESCE(SUM(CASE WHEN b.state='READY' THEN b.qty_available END), 0) ready,
                   COALESCE(SUM(CASE WHEN b.state='DRYING' THEN b.qty_available END), 0) drying,
                   COALESCE(SUM(CASE WHEN b.state='RESERVED' THEN b.qty_available END), 0) reserved,
                   COUNT(b.box_id) boxes
            FROM articles a LEFT JOIN boxes b ON b.article_ref = a.ref
            GROUP BY a.ref ORDER BY a.ref
        """)

        # the full rack (all C.SLOT_COUNT cells), joined to whatever box currently occupies it —
        # this is the literal content of the `slots` table, laid out the way the
        # physical room is laid out, so it doubles as a live audit of that table.
        grid = DB.rows(con, """
            SELECT s.slot_id, s.face, s.col, s.level,
                   b.box_id, b.article_ref, b.state, b.qty_available
            FROM slots s LEFT JOIN boxes b ON b.box_id = s.occupied_by
            ORDER BY s.face, s.col, s.level
        """)

        events_total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        latest_event = DB.one(con, "SELECT t_sim, kind FROM events ORDER BY id DESC LIMIT 1")
        orders_by_status = {r["status"]: r["n"] for r in DB.rows(
            con, "SELECT status, COUNT(*) n FROM orders GROUP BY status")}

        return {
            "by_state": by_state,
            "by_ref": by_ref,
            "grid": grid,
            "events_total": events_total,
            "latest_event": latest_event,
            "orders_by_status": orders_by_status,
        }


@router.get("/check")
def check():
    """Read-only consistency badge (backend/consistency.py) -- PASS/WARN/FAIL
    across 22 checks. `now_sim` is inferred from the newest event timestamp
    since this router has no access to main.py's live SimClock (dbview.py is
    deliberately stateless -- every endpoint here opens its own connection)."""
    with _con() as con:
        return CHECK.run_checks(con)


@router.get("/box/{box_id}")
def box_trace(box_id: str):
    """Everything the database knows about one box: its row, its slot, every
    event that names it, and every order whose picks include it -- the
    answer to "why is BOX-12 in quarantine?" the plan's event-design section
    asks for, in one call instead of four manual queries."""
    with _con() as con:
        box = DB.one(con, "SELECT * FROM boxes WHERE box_id=?", (box_id,))
        if not box:
            raise HTTPException(404, "unknown box: %s" % box_id)
        slot = (DB.one(con, "SELECT * FROM slots WHERE slot_id=?", (box["slot_id"],))
               if box["slot_id"] else None)
        def _mentions_box(payload: dict) -> bool:
            if payload.get("box_id") == box_id:
                return True
            # "picks"/"released": list of box_id strings (demand, order_cancel,
            # order_expired). "boxes": list of {"box_id": ..., ...} dicts
            # (pick_done's per-box detail).
            for key in ("picks", "released"):
                if box_id in (payload.get(key) or []):
                    return True
            return any(d.get("box_id") == box_id for d in payload.get("boxes") or [])

        events = []
        for e in DB.rows(con, "SELECT * FROM events ORDER BY id"):
            try:
                payload = json.loads(e["payload"])
            except Exception:
                continue
            if _mentions_box(payload):
                events.append({"id": e["id"], "t_sim": e["t_sim"],
                              "kind": e["kind"], "payload": payload})
        orders = []
        for o in DB.rows(con, "SELECT * FROM orders"):
            try:
                plan = json.loads(o["payload"])
            except Exception:
                continue
            if any(p.get("box_id") == box_id for p in plan.get("picks", [])):
                orders.append({"order_id": o["order_id"], "status": o["status"],
                              "ref": o["ref"], "created_sim": o["created_sim"]})
        return {"box": box, "slot": slot, "events": events, "orders": orders}
