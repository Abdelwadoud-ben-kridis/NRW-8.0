"""
algo/engine.py — Smart Core Warehouse decision engine.

PURE FUNCTIONS ONLY. No sqlite, no fastapi, no sockets, no printing, no clock.
Everything that decides anything lives here so it can be unit-tested in
milliseconds and demoed on a whiteboard.

This is P2's module. P3 (backend) only calls into it.

Vocabulary
----------
t_sim            simulated time, SECONDS. Never a wall clock.
article          a noyau reference. Its unit_mass_g is GIVEN ground truth.
box              a plastic crate holding N cores of one article.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants (must match firmware/sketch.ino and docs/contracts.md)
# ---------------------------------------------------------------------------

TARE_G = 1800.0            # empty crate 51.5x32.5x17.5 cm, 1.8 kg

# CDC hard requirement: every box dries exactly 24 h, no exceptions. This is
# NOT a floor for an adaptive model -- the CDC (cahier des charges) never asks
# for one, and contract 1.2 drops the earlier adaptive-cure idea entirely.
# Temperature/RH are kept only as arrival evidence on the box (what the ESP32
# measured at that moment) and displayed on the HMI; they never feed cure
# math. See docs/contracts.md CONTRACT VERSION 1.2 and the plan's decision
# log for why (2026-09-12).
CURE_FLOOR_H = 24.0

# a RESERVED box auto-releases after this many SIMULATED hours if nobody
# confirms or cancels the order (also mirrored in backend/config.py::LOCK_TTL_H,
# which is what the backend actually uses -- this default is for callers of
# this module in isolation, e.g. tests and a whiteboard).
LOCK_TTL_H = 2.0

STATES = (
    "INCOMING", "IDENTIFYING", "COUNTING", "STORING", "DRYING", "READY",
    "RESERVED", "PICKING", "EMPTY", "ARCHIVED", "QUARANTINE",
)

# States actually WRITTEN to boxes.state by this system. INCOMING,
# IDENTIFYING, COUNTING, STORING and PICKING are virtual/conceptual stages --
# named in docs/contracts.md and in event payloads for narrative clarity, but
# a box is born directly into DRYING or QUARANTINE, and a full pick moves
# RESERVED straight to EMPTY without ever being written as PICKING. See
# docs/database-guide.md for the persisted-vs-virtual distinction.
PERSISTED_STATES = ("DRYING", "READY", "RESERVED", "EMPTY", "QUARANTINE",
                    "ARCHIVED")

# states a box must NOT be in to be pickable, with the reason shown to the jury
_NOT_PICKABLE = {
    "INCOMING":    ("en cours de reception",   "pas encore stocke"),
    "IDENTIFYING": ("en cours de reception",   "identification en cours"),
    "COUNTING":    ("en cours de reception",   "comptage en cours"),
    "STORING":     ("en cours de reception",   "transfert vers le rack"),
    "DRYING":      ("sechage insuffisant",     ""),          # detail filled in later
    "RESERVED":    ("reserve",                 ""),          # detail = order id
    "PICKING":     ("prelevement en cours",    ""),
    "EMPTY":       ("vide",                    ""),
    "ARCHIVED":    ("archive",                 ""),
    "QUARANTINE":  ("quarantaine",             ""),
}


# ---------------------------------------------------------------------------
# 1. Cure model  (criterion 4)
# ---------------------------------------------------------------------------
# Fixed 24 h for every box, every reference, every climate -- the CDC's exact
# requirement, no more and no less. An earlier draft explored an adaptive
# model that extended the requirement in a cold/humid room; the CDC does not
# ask for that, so it was dropped in contract 1.2. Temperature/RH remain on
# the HMI and on each box's arrival evidence for traceability, but are pure
# display -- nothing here reads them.

def required_cure_h() -> float:
    """The one cure requirement in this system: exactly 24 h, always.

    >>> required_cure_h()
    24.0
    """
    return CURE_FLOOR_H


def cure_progress(t_in_sim: float, required_h: float, now_sim: float) -> float:
    """0..100 % of the cure elapsed. Clamped."""
    if required_h <= 0:
        return 100.0
    pct = (now_sim - t_in_sim) / (required_h * 3600.0) * 100.0
    return max(0.0, min(100.0, pct))


def is_cured(t_in_sim: float, required_h: float, now_sim: float) -> bool:
    return now_sim >= t_in_sim + required_h * 3600.0


def hours_remaining(t_in_sim: float, required_h: float, now_sim: float) -> float:
    return max(0.0, (t_in_sim + required_h * 3600.0 - now_sim) / 3600.0)


# ---------------------------------------------------------------------------
# 2. Identification + quantity  (criteria 2 and 3)
# ---------------------------------------------------------------------------

def count_from_weight(gross_g: float, unit_mass_g: float,
                      tare_g: float = TARE_G) -> int:
    """Core count derived from mass alone: (gross - tare) / unit_mass_g.

    `unit_mass_g` is that SPECIFIC box's own registered per-noyau weight
    (backend/warehouse.py::register_barcode), not a shared article average --
    each physical box's barcode carries its own measured value.
    """
    if unit_mass_g <= 0:
        return 0
    return max(0, round((gross_g - tare_g) / unit_mass_g))


def assess_box(barcode: dict, article: dict, gross_g: float,
               tare_g: float = TARE_G) -> dict:
    """Count a box from the scale alone.

    Identification already happened before this ever runs: the conveyor's
    barcode scanner read `barcode_id` and backend/warehouse.py::create_box
    looked it up, so the reference and per-noyau weight are GIVEN, not
    guessed -- an unregistered or already-used barcode never reaches this
    function at all (it's quarantined one level up, in create_box).

    The scale is the only sensor left, so there is only one question left to
    ask: does the net mass look like a clean whole number of cores at THIS
    box's own registered weight?

        count    = round((gross - tare) / barcode's unit_mass_g)
        residual = |net - count * unit_mass_g|

    A small residual means the physical contents match what the barcode
    promised. A large one means a mismatch -- cores swapped after labelling,
    the wrong sticker on the wrong box, or a weight mismeasured at
    registration -- and it is caught here with no second sensor needed: the
    weight simply will not line up with any clean multiple of the registered
    per-noyau mass. Honest limitation: for a very light reference, a
    mismatched box can coincidentally land near a multiple of the wrong
    weight and slip through; there is no second measurement here to catch
    that, by design (finding: the earlier beam+scale cross-check gave that
    protection at the cost of hardware complexity the CDC never asked for).

    Returns a dict ready to be written straight into the `boxes` row:
        {count_weight, quantity, confidence, accepted, state, reason,
         unit_mass_measured}
    """
    unit = float(barcode["unit_mass_g"])
    tol = float(article.get("tolerance_g", 5.0))
    net = gross_g - tare_g
    count = count_from_weight(gross_g, unit, tare_g)
    residual = abs(net - count * unit)
    id_tol = max(tol, 0.12 * unit)          # covers load-cell noise

    out = {
        "count_weight": int(count),
        "gross_g": float(gross_g),
        "unit_mass_measured": round((net / count), 1) if count > 0 else 0.0,
    }

    if count <= 0 or residual > id_tol:
        out.update(quantity=0, confidence="NULLE", accepted=False,
                   state="QUARANTINE",
                   reason=("masse nette %.0f g incompatible avec le code-barre "
                           "%s (%.1f g/noyau attendu, ecart %.0f g)"
                           % (net, barcode["barcode_id"], unit, residual)))
        return out

    out.update(quantity=count, confidence="HAUTE", accepted=True,
               state="STORING", reason=None)

    # --- CAPACITY --------------------------------------------------------------
    # A crate cannot physically hold more cores than its declared capacity.
    capacity = int(article.get("box_capacity") or 0)
    if capacity > 0 and out["quantity"] > capacity:
        out.update(accepted=False, state="QUARANTINE",
                   reason="quantite %d superieure a la capacite de la caisse (%d)"
                          % (out["quantity"], capacity))
    return out


# ---------------------------------------------------------------------------
# 3. Slot assignment  (criterion 5, the "locate" half)
# ---------------------------------------------------------------------------

def choose_slot(free_slots: list, ref: str) -> dict | None:
    """Pick the best free slot for an incoming box.

    Policy: lowest level first (a fragile pre-cure crate should travel as
    little vertical distance as possible), then lowest column (shortest crane
    X travel), then face 0. Deterministic -> the demo behaves the same twice.
    """
    if not free_slots:
        return None
    return sorted(free_slots,
                  key=lambda s: (s["level"], s["col"], s["face"]))[0]


# ---------------------------------------------------------------------------
# 4. FIFO allocation  (criteria 5 + 6 — 30 pts, the heart of the demo)
# ---------------------------------------------------------------------------

_ID_NUM_RE = re.compile(r"(\d+)$")


def fifo_key(box: dict) -> tuple:
    """The one true FIFO sort key: (t_in_sim, box sequence).

    docs/contracts.md says the tiebreak is "box_id", but box_id ("BOX-2",
    "BOX-10", ...) must be compared as the NATURAL NUMBER it encodes, not as
    a string -- otherwise "BOX-10" sorts before "BOX-2" and two boxes stored
    in the same simulated instant (speed 0, or a scenario load) tiebreak
    backwards. Every place that orders boxes for FIFO purposes (allocation,
    the inventory table, the by-ref FIFO head) must use this same key so the
    three views of "what's next" never disagree.

    Falls back to the raw id string if it has no trailing digits, so an
    unexpected id shape cannot raise -- it just loses the numeric tiebreak.
    """
    bid = box["box_id"]
    m = _ID_NUM_RE.search(bid)
    return (box["t_in_sim"], int(m.group(1)) if m else bid)


def fifo_allocate(boxes: list, ref: str, qty: int, now_sim: float,
                  order_id: str = "ORD-0") -> dict:
    """Answer the CDC's question: which box do I use first, and why not the others?

    `boxes` = every box currently in the warehouse (any ref, any state).
    Returns the contract shape documented in docs/contracts.md section 4.

    Boxes are shipped WHOLE, never split: a pick always takes a candidate
    box's entire qty_available, so a box can never be left half-consumed
    (contract 1.3). Because of that, `qty_allocated` may legitimately exceed
    `qty_requested` -- covering an order can require rounding up to the next
    whole box. And because a box can't be split to make up a shortfall, the
    total WHOLE-box stock for `ref` must already cover `qty` before anything
    is reserved: if it doesn't, nothing is picked at all (status IMPOSSIBLE)
    rather than silently handing out less than what was asked for.

    The REJECTED list is the deliverable. Anyone can sort a list by date; what
    convinces a jury is showing, per box, the reason it was passed over.
    """
    rejected = []
    needed = int(qty)

    candidates = [b for b in boxes if b["article_ref"] == ref]
    # deterministic FIFO key: oldest stored first, numeric box_id breaks ties
    candidates.sort(key=fifo_key)

    pickable = []
    for b in candidates:
        state = b["state"]

        # self-healing: a box already past its 24 h floor is pickable even if
        # its state column still says DRYING -- sweep_cured only flips the
        # column on loop_clock's next 0.2 s tick, and demand must not depend
        # on winning that race (e.g. D pressed right after a clock jump/J)
        if state == "DRYING" and is_cured(b["t_in_sim"], b["required_cure_h"], now_sim):
            state = "READY"

        if state in _NOT_PICKABLE:
            label, detail = _NOT_PICKABLE[state]
            if state == "DRYING":
                detail = "pret dans %.1f h" % hours_remaining(
                    b["t_in_sim"], b["required_cure_h"], now_sim)
            elif state == "RESERVED":
                detail = b.get("locked_by") or "autre commande"
            elif state == "QUARANTINE":
                detail = b.get("reason") or "controle de coherence"
            rejected.append({"box_id": b["box_id"], "reason": label,
                             "detail": detail, "t_in_sim": b["t_in_sim"]})
            continue

        if state != "READY":
            rejected.append({"box_id": b["box_id"], "reason": "indisponible",
                             "detail": state, "t_in_sim": b["t_in_sim"]})
            continue

        # belt and braces: never hand out a box that has not met the 24 h floor,
        # whatever its state column says
        if not is_cured(b["t_in_sim"], b["required_cure_h"], now_sim):
            rejected.append({
                "box_id": b["box_id"], "reason": "sechage insuffisant",
                "detail": "pret dans %.1f h" % hours_remaining(
                    b["t_in_sim"], b["required_cure_h"], now_sim),
                "t_in_sim": b["t_in_sim"]})
            continue

        avail = int(b["qty_available"])
        if avail <= 0:
            rejected.append({"box_id": b["box_id"], "reason": "vide",
                             "detail": "0 noyau", "t_in_sim": b["t_in_sim"]})
            continue

        pickable.append(b)

    total_avail = sum(int(b["qty_available"]) for b in pickable)

    if total_avail < needed:
        # Not enough WHOLE boxes to cover the request -- refuse the whole
        # reservation instead of reserving whatever is available. Every
        # otherwise-pickable box still shows up in `rejected`, so the jury
        # sees a stock problem, not a state problem.
        for b in pickable:
            rejected.append({
                "box_id": b["box_id"], "reason": "stock insuffisant",
                "detail": "%d disponible(s) au total pour %d demande(s)"
                          % (total_avail, needed),
                "t_in_sim": b["t_in_sim"]})
        return {
            "order_id": order_id, "ref": ref, "qty_requested": needed,
            "qty_allocated": 0, "shortfall": needed,
            "picks": [], "rejected": rejected, "status": "IMPOSSIBLE",
        }

    picks = []
    taken = 0
    for b in pickable:
        if taken >= needed:
            rejected.append({"box_id": b["box_id"], "reason": "plus recent (FIFO)",
                             "detail": "besoin deja couvert par des box plus anciennes",
                             "t_in_sim": b["t_in_sim"]})
            continue
        avail = int(b["qty_available"])
        picks.append({"box_id": b["box_id"], "slot_id": b.get("slot_id"),
                      "take": avail, "t_in_sim": b["t_in_sim"],
                      "rank": len(picks) + 1, "partial": False})
        taken += avail

    return {
        "order_id": order_id,
        "ref": ref,
        "qty_requested": needed,
        "qty_allocated": taken,
        "shortfall": max(0, needed - taken),
        "picks": picks,
        "rejected": rejected,
        "status": "PENDING" if picks else "IMPOSSIBLE",
    }


# ---------------------------------------------------------------------------
# 5. State machine
# ---------------------------------------------------------------------------
# This table lists only transitions between PERSISTED states (see
# PERSISTED_STATES above). `None` as the "old" state means "box creation" --
# a box is born directly into DRYING or QUARANTINE; the conceptual
# INCOMING -> IDENTIFYING -> COUNTING -> STORING chain from docs/contracts.md
# is never written to the database, only narrated in the creation event's
# payload. Likewise RESERVED -> EMPTY is a direct write: PICKING is a
# real-world stage (the crane is physically extracting cores) but this
# system never persists it as a box.state value -- the whole
# reserve-then-pick sequence is one atomic backend operation, not two.
_TRANSITIONS = {
    None:          {"DRYING", "QUARANTINE"},
    "DRYING":      {"READY", "QUARANTINE"},
    "READY":       {"RESERVED", "QUARANTINE", "ARCHIVED"},
    "RESERVED":    {"READY", "EMPTY"},            # READY: cancel/expiry/partial pick
    "EMPTY":       {"ARCHIVED"},
    "QUARANTINE":  {"ARCHIVED"},                  # operator re-presentation is out of
                                                   # scope: no endpoint implements it
    "ARCHIVED":    set(),
}


def can_transition(old: str | None, new: str) -> bool:
    """Is `old -> new` a legal PERSISTED box.state change?

    Pass `old=None` to check whether `new` is a legal birth state.
    """
    return new in _TRANSITIONS.get(old, set())


# Order lifecycle, mirroring the box table above. `None` -> the two possible
# outcomes of a fresh POST /api/demand. PENDING -> DONE is a confirm;
# PENDING -> CANCELLED covers both an explicit cancel and an automatic
# reservation-lock expiry (backend/warehouse.py logs which one happened).
# Re-confirming a DONE order or re-cancelling a CANCELLED one is deliberately
# NOT a transition here -- backend/warehouse.py treats repeating either as a
# harmless no-op instead of looking it up in this table, so idempotency is
# handled once, explicitly, rather than by quietly allowing DONE -> DONE.
_ORDER_TRANSITIONS = {
    None:         {"PENDING", "IMPOSSIBLE"},
    "PENDING":    {"DONE", "CANCELLED"},
    "IMPOSSIBLE": set(),
    "DONE":       set(),
    "CANCELLED":  set(),
}


def can_transition_order(old: str | None, new: str) -> bool:
    return new in _ORDER_TRANSITIONS.get(old, set())


def tick_box(box: dict, now_sim: float) -> dict | None:
    """Time-driven transitions. Returns a patch dict, or None if nothing changes.

    Two things happen on their own as simulated time passes:
      * a DRYING box becomes READY when its adaptive cure time has elapsed
      * a RESERVED box whose lock expired falls back to READY
    """
    st = box["state"]
    if st == "DRYING" and is_cured(box["t_in_sim"], box["required_cure_h"], now_sim):
        return {"state": "READY"}
    if st == "RESERVED":
        exp = box.get("lock_expires_sim")
        if exp is not None and now_sim >= exp:
            return {"state": "READY", "locked_by": None, "lock_expires_sim": None}
    return None


def apply_pick(box: dict, take: int) -> dict:
    """Consume `take` cores. Partial picks keep t_in_sim — re-stamping breaks FIFO.

    Raises ValueError if `take` is not a positive integer no greater than
    qty_available. The caller (backend/warehouse.py) treats that as a failed
    precondition inside a transaction and rolls the whole confirm back,
    rather than silently clamping to whatever was left -- a stale or
    tampered-with order payload must never short-change or over-deduct a box.
    """
    avail = int(box["qty_available"])
    take = int(take)
    if take <= 0 or take > avail:
        raise ValueError("invalid take %r for box %s (qty_available=%d)"
                         % (take, box.get("box_id"), avail))
    left = avail - take
    return {
        "qty_available": left,
        "state": "EMPTY" if left == 0 else "READY",
        "locked_by": None,
        "lock_expires_sim": None,
        # t_in_sim deliberately untouched
    }


# ---------------------------------------------------------------------------
# 6. Reservation-lock expiry  (pure predicate, the clock/DB stay in warehouse.py)
# ---------------------------------------------------------------------------

def lock_expired(box: dict, now_sim: float) -> bool:
    """Has this RESERVED box's lock run out?

    Used by backend/warehouse.py to find whole ORDERS to expire (every box an
    order reserved shares that order's lock_expires_sim, since demand() sets
    them together) -- this function only answers the box-level question.
    """
    if box.get("state") != "RESERVED":
        return False
    exp = box.get("lock_expires_sim")
    return exp is not None and now_sim >= exp


# ---------------------------------------------------------------------------
# 7. Arrival dedup  (criterion: one physical box_done -> one database box)
# ---------------------------------------------------------------------------
# Pure verdict functions only -- backend/main.py owns the actual arrival
# window (open/resolved, epoch) and the monotonic clock; this module just
# answers "given this window and this incoming message, what should happen?"
# so the decision itself is unit-testable without MQTT, asyncio or a clock.
# See docs/contracts.md CONTRACT VERSION 1.2 §1.3 for the rationale: nothing
# new is added to the box_done payload, so this works with any device
# (Wokwi, tools/fake_device.py, the real board) unmodified.

def box_fingerprint(barcode_id: str, gross_g: float, fw: str | None) -> tuple:
    """A cheap identity for a physical box_done payload, gram rounded to 0.1
    so the plant model's noise cannot make one real box look like two."""
    return (barcode_id, round(float(gross_g), 1), fw or "")


def dedup_verdict(window: dict | None, fp: tuple, barcode_id: str, now_mono: float,
                  last_unsolicited: tuple | None, grace_s: float,
                  unsolicited_dedup_s: float) -> dict:
    """Decide what an incoming box_done should do to the database.

    window   -- the arrival window this backend is tracking for the box
                currently expected on the conveyor, or None:
                {"barcode_id": str, "status": "OPEN"|"RESOLVED_L0"|"RESOLVED_L1",
                 "resolved_at": float|None}
    fp       -- box_fingerprint(...) of the incoming message
    barcode_id -- the incoming message's own identity field (still named
                "ref" on the wire for firmware-compatibility -- the board
                only ever echoes it back, never parses it, so nothing there
                needed to change when identification moved to a barcode
                scan; see docs/contracts.md CONTRACT VERSION 1.5)
    now_mono -- time.monotonic() at receipt (transport domain, never t_sim)
    last_unsolicited -- (fingerprint, mono_time) of the last box accepted
                with no open window, or None if there hasn't been one yet

    Returns {"action": "create" | "resolve_window" | "ignore",
             "reason": str | None}

        create         -- store a new box (no window, or window not a match)
        resolve_window -- store a new box AND mark the window resolved (this
                          is the normal L0 path: the device answered in time)
        ignore         -- a duplicate; do not touch the database
    """
    if (window is not None and window["status"] == "OPEN"
            and window["barcode_id"] == barcode_id):
        return {"action": "resolve_window", "reason": None}

    if window is not None and window["status"] in ("RESOLVED_L0", "RESOLVED_L1"):
        resolved_at = window.get("resolved_at")
        if resolved_at is not None and (now_mono - resolved_at) <= grace_s:
            return {"action": "ignore",
                    "reason": "duplicate: this arrival was already resolved "
                             "(%s)" % window["status"]}

    if last_unsolicited is not None:
        last_fp, last_t = last_unsolicited
        if last_fp == fp and (now_mono - last_t) <= unsolicited_dedup_s:
            return {"action": "ignore",
                    "reason": "identical unsolicited box_done repeated "
                             "within %.0f s" % unsolicited_dedup_s}

    return {"action": "create", "reason": None}
