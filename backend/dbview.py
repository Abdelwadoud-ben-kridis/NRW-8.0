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

from fastapi import APIRouter, HTTPException, Query

from backend import db as DB

router = APIRouter()

# Whitelisted — table names are never taken from the request as raw SQL.
TABLES = ("articles", "boxes", "slots", "orders", "events")

# Hand-described schema: PRAGMA gives us columns/types/FKs accurately, but a
# jury (or you, at 3 a.m.) reads the RELATIONSHIP faster from a sentence than
# from a constraint name. Kept next to the live introspection below, not
# instead of it, so this file breaks loudly if the two ever disagree.
RELATIONS = [
    {"from": "boxes.article_ref", "to": "articles.ref", "kind": "fk",
     "note": "every box holds cores of exactly one reference"},
    {"from": "boxes.slot_id", "to": "slots.slot_id", "kind": "fk",
     "note": "a box occupies at most one slot (NULL while incoming/quarantined)"},
    {"from": "slots.occupied_by", "to": "boxes.box_id", "kind": "soft",
     "note": "back-reference kept in sync with boxes.slot_id, not FK-enforced"},
    {"from": "slots.reserved_for", "to": "orders.order_id", "kind": "soft",
     "note": "set while a pick is RESERVED; cleared on confirm/cancel/expiry"},
    {"from": "orders.payload", "to": "boxes.box_id", "kind": "json",
     "note": "the FIFO plan (picks[] / rejected[]) is stored as a JSON blob, not rows"},
    {"from": "events.payload", "to": "*", "kind": "json",
     "note": "append-only log; every mutation above also writes one event row"},
]


def _con():
    con = DB.connect()
    con.execute("PRAGMA query_only = ON")   # belt-and-braces: refuse writes at the driver level
    return con


def _check_table(name: str) -> str:
    if name not in TABLES:
        raise HTTPException(404, "unknown table: %s" % name)
    return name


@router.get("/schema")
def schema():
    con = _con()
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
    con.close()
    return {"tables": tables, "relations": RELATIONS}


@router.get("/tables")
def tables():
    con = _con()
    out = [{"name": t, "row_count": con.execute(
        "SELECT COUNT(*) FROM %s" % t).fetchone()[0]} for t in TABLES]
    con.close()
    return out


@router.get("/table/{name}")
def table(name: str, limit: int = Query(50, ge=1, le=500),
          offset: int = Query(0, ge=0), q: str = Query("")):
    name = _check_table(name)
    con = _con()
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
    con.close()
    return {"table": name, "columns": cols, "rows": rows,
           "total": total, "limit": limit, "offset": offset}


@router.get("/stats")
def stats():
    con = _con()

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

    con.close()
    return {
        "by_state": by_state,
        "by_ref": by_ref,
        "grid": grid,
        "events_total": events_total,
        "latest_event": latest_event,
        "orders_by_status": orders_by_status,
    }
