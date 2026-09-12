"""SQLite layer. Thin on purpose: all decisions live in algo/engine.py."""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config as C

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS articles (
  ref           TEXT PRIMARY KEY,
  label         TEXT NOT NULL,
  unit_mass_g   REAL NOT NULL,
  tolerance_g   REAL NOT NULL DEFAULT 6.0,
  box_capacity  INTEGER NOT NULL DEFAULT 40,
  cure_floor_h  REAL NOT NULL DEFAULT 24.0,
  color         TEXT DEFAULT '#4f8cff'
);

CREATE TABLE IF NOT EXISTS slots (
  slot_id     TEXT PRIMARY KEY,
  face        INTEGER NOT NULL,
  col         INTEGER NOT NULL,
  level       INTEGER NOT NULL,
  occupied_by TEXT,
  reserved_for TEXT
);

CREATE TABLE IF NOT EXISTS boxes (
  box_id          TEXT PRIMARY KEY,
  article_ref     TEXT NOT NULL REFERENCES articles(ref),
  qty_initial     INTEGER NOT NULL,
  qty_available   INTEGER NOT NULL,
  slot_id         TEXT REFERENCES slots(slot_id),
  state           TEXT NOT NULL,
  t_in_sim        REAL NOT NULL,
  required_cure_h REAL NOT NULL,
  ready_at_sim    REAL NOT NULL,
  count_beam      INTEGER DEFAULT 0,
  count_weight    INTEGER DEFAULT 0,
  gross_g         REAL DEFAULT 0,
  confidence      TEXT DEFAULT 'HAUTE',
  reason          TEXT,
  locked_by       TEXT,
  lock_expires_sim REAL
);

CREATE TABLE IF NOT EXISTS orders (
  order_id      TEXT PRIMARY KEY,
  ref           TEXT NOT NULL,
  qty_requested INTEGER NOT NULL,
  qty_allocated INTEGER NOT NULL,
  status        TEXT NOT NULL,
  created_sim   REAL NOT NULL,
  payload       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  t_sim   REAL NOT NULL,
  kind    TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_boxes_ref_state ON boxes(article_ref, state);
CREATE INDEX IF NOT EXISTS idx_boxes_tin ON boxes(t_in_sim);
"""

ARTICLES = [
    # ref,     label,                       unit g, tol, cap, color
    ("NY-114", "Noyau culasse 114",          206.0, 6.0, 40, "#4f8cff"),
    ("NY-220", "Noyau corps de vanne 220",   412.0, 9.0, 24, "#22c98a"),
    ("NY-075", "Noyau raccord 75",            88.5, 4.0, 60, "#f5a623"),
    ("NY-330", "Noyau collecteur 330",       735.0, 12.0, 12, "#c86bfa"),
]

# colors offered to a reference created from the UI, cycled so a jury demo
# never ends up with two references sharing one swatch by accident
NEW_REF_PALETTE = ["#4f8cff", "#22c98a", "#f5a623", "#c86bfa",
                   "#ff8fa3", "#4dd0e1", "#a3e635", "#f472b6"]


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or C.DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def slot_id(face: int, col: int, level: int) -> str:
    return "F%d-C%d-L%d" % (face, col, level)


def seed(con: sqlite3.Connection) -> None:
    """Wipe the dynamic tables and lay out a fresh rack (C.SLOT_COUNT slots)."""
    con.executescript("""
        DELETE FROM boxes; DELETE FROM orders; DELETE FROM events;
        DELETE FROM slots; DELETE FROM articles; DELETE FROM meta;
    """)
    con.executemany(
        "INSERT INTO articles(ref,label,unit_mass_g,tolerance_g,box_capacity,color)"
        " VALUES (?,?,?,?,?,?)", ARTICLES)
    rows = []
    for f in range(C.FACES):
        for c in range(1, C.COLS + 1):
            for l in range(1, C.LEVELS + 1):
                rows.append((slot_id(f, c, l), f, c, l))
    con.executemany(
        "INSERT INTO slots(slot_id,face,col,level) VALUES (?,?,?,?)", rows)
    con.commit()


# --- tiny helpers ------------------------------------------------------------

def rows(con, sql, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()) -> dict | None:
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def log_event(con, t_sim: float, kind: str, payload: dict) -> None:
    con.execute("INSERT INTO events(t_sim,kind,payload) VALUES (?,?,?)",
                (t_sim, kind, json.dumps(payload, ensure_ascii=False)))
    con.commit()


def next_id(con, table: str, col: str, prefix: str) -> str:
    n = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    while True:
        n += 1
        cand = "%s-%d" % (prefix, n)
        if not con.execute("SELECT 1 FROM %s WHERE %s=?" % (table, col),
                           (cand,)).fetchone():
            return cand


def update(con, table: str, key_col: str, key: str, patch: dict) -> None:
    if not patch:
        return
    sets = ", ".join("%s=?" % k for k in patch)
    con.execute("UPDATE %s SET %s WHERE %s=?" % (table, sets, key_col),
                list(patch.values()) + [key])


def add_article(con, ref: str, label: str, unit_mass_g: float,
                tolerance_g: float | None = None,
                box_capacity: int = 40, cure_floor_h: float = 24.0,
                color: str | None = None) -> dict:
    """Register a new reference/type. Raises ValueError on a bad or
    duplicate ref -- the caller (the REST handler) turns that into a 400."""
    ref = (ref or "").strip()
    label = (label or "").strip()
    if not ref or not label:
        raise ValueError("ref et label sont obligatoires")
    if one(con, "SELECT 1 FROM articles WHERE ref=?", (ref,)):
        raise ValueError("reference deja existante: %s" % ref)
    if unit_mass_g <= 0:
        raise ValueError("masse unitaire doit etre > 0")
    if tolerance_g is None:
        tolerance_g = round(max(1.0, unit_mass_g * 0.03), 1)   # ~3 % default
    if not color:
        n = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        color = NEW_REF_PALETTE[n % len(NEW_REF_PALETTE)]
    con.execute(
        "INSERT INTO articles(ref,label,unit_mass_g,tolerance_g,"
        "box_capacity,cure_floor_h,color) VALUES (?,?,?,?,?,?,?)",
        (ref, label, float(unit_mass_g), float(tolerance_g),
         int(box_capacity), float(cure_floor_h), color))
    con.commit()
    return one(con, "SELECT * FROM articles WHERE ref=?", (ref,))


if __name__ == "__main__":
    c = connect()
    init(c)
    seed(c)
    print("seeded %s: %d slots, %d articles" % (
        C.DB_PATH, C.SLOT_COUNT, len(ARTICLES)))
