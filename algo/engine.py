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

# ---------------------------------------------------------------------------
# Constants (must match firmware/sketch.ino and docs/contracts.md)
# ---------------------------------------------------------------------------

TARE_G = 1800.0            # empty crate 51.5x32.5x17.5 cm, 1.8 kg
CURE_FLOOR_H = 24.0        # CDC hard requirement: >= 24 h. Never violated.
LOCK_TTL_H = 0.5           # a RESERVED box auto-releases after 30 simulated min

STATES = (
    "INCOMING", "IDENTIFYING", "COUNTING", "STORING", "DRYING", "READY",
    "RESERVED", "PICKING", "EMPTY", "ARCHIVED", "QUARANTINE",
)

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
# 1. Cure model  (criterion 4, and the primary innovation)
# ---------------------------------------------------------------------------

def required_cure_h(t_c: float, rh: float, floor_h: float = CURE_FLOOR_H) -> float:
    """Adaptive cure time from curing-room climate.

    The CDC fixes a 24 h minimum. Real resin-bonded sand cures slower when the
    room is cold or humid, so a fixed 24 h is either wasteful or unsafe. This
    model can only EXTEND the requirement, never shorten it below the floor.

        k_rh : +1.2 % per point of RH above 45 %
        k_t  : +2.0 % per degree below 22 C

    Both factors are clamped at 1.0 on the favourable side, so a warm dry room
    gives exactly the 24 h floor and nothing shorter.

    >>> round(required_cure_h(22.0, 45.0), 2)
    24.0
    >>> round(required_cure_h(16.0, 80.0), 1)
    38.2
    """
    k_rh = 1.0 + max(0.0, rh - 45.0) * 0.012
    k_t = 1.0 + max(0.0, 22.0 - t_c) * 0.020
    return max(floor_h, floor_h * k_rh * k_t)


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
    """Second, independent core count, derived from mass alone.

    Valid here because each reference's unit mass is GIVEN ground truth in this
    simulation, not variable manufacturing output.
    """
    if unit_mass_g <= 0:
        return 0
    return max(0, round((gross_g - tare_g) / unit_mass_g))


def assess_box(article: dict, count_beam: int, gross_g: float,
               tare_g: float = TARE_G) -> dict:
    """Cross-check two independent measurements of the same box.

    measurement A : count_beam   — photoelectric barrier event count
    measurement B : count_weight — (gross - tare) / unit_mass_g

    Returns a dict ready to be written straight into the `boxes` row:
        {count_weight, quantity, confidence, accepted, state, reason,
         unit_mass_measured, delta}

    Decision table
    --------------
    Identification runs FIRST, and asks one question: is the net mass an
    integer multiple of the declared reference's unit mass? A correctly
    labelled crate always is, whatever the barrier counted. A mislabelled one
    is not. That ordering matters -- it keeps "wrong part" and "miscounted"
    as two distinct diagnoses instead of blaming whichever fires first.

        net not a multiple of unit_mass -> QUARANTINE, wrong reference
        |A-B| == 0  -> ACCEPTED, qty = A,          confidence HAUTE
        |A-B| == 1  -> ACCEPTED, qty = min(A,B),   confidence MOYENNE
        |A-B| >= 2  -> QUARANTINE, counting fault

    Honest limitation: for a very light reference, a mislabelled crate can
    land near a multiple of the declared unit mass by coincidence. It is then
    caught by the count disagreement instead. Either way the crate is
    quarantined -- only the wording of the reason differs.
    """
    unit = float(article["unit_mass_g"])
    tol = float(article.get("tolerance_g", 5.0))
    net = gross_g - tare_g
    count_weight = count_from_weight(gross_g, unit, tare_g)
    delta = abs(count_beam - count_weight)
    unit_measured = (net / count_beam) if count_beam > 0 else 0.0

    out = {
        "count_beam": int(count_beam),
        "count_weight": int(count_weight),
        "gross_g": float(gross_g),
        "delta": int(delta),
        "unit_mass_measured": round(unit_measured, 1),
    }

    # --- IDENTIFICATION ------------------------------------------------------
    # The question is NOT "does net/count_beam equal the unit mass" -- that
    # conflates two different faults. If the barrier miscounted, net/count_beam
    # is wrong even though the crate is correctly labelled.
    #
    # The clean discriminator: is the net mass an integer multiple of the
    # DECLARED reference's unit mass? Any correctly-labelled crate must be,
    # whatever the barrier saw. A crate of some other part is not.
    residual = abs(net - count_weight * unit)
    id_tol = max(tol, 0.12 * unit)          # covers load-cell noise
    if net > unit * 0.5 and residual > id_tol:
        out.update(quantity=0, confidence="NULLE", accepted=False,
                   state="QUARANTINE",
                   reason=("masse nette %.0f g incompatible avec %s : "
                           "aucun multiple entier de %.1f g (ecart %.0f g)"
                           % (net, article["ref"], unit, residual)))
        return out

    if count_beam == 0 and net > unit * 0.5:    # mass present, barrier saw none
        out.update(quantity=0, confidence="NULLE", accepted=False,
                   state="QUARANTINE",
                   reason="masse detectee mais aucun passage barriere (capteur HS ?)")
        return out

    # --- QUANTITY ------------------------------------------------------------
    # The reference is confirmed, so a disagreement between the two counts is a
    # COUNTING fault, and it is reported as one.
    if delta == 0:
        out.update(quantity=count_beam, confidence="HAUTE", accepted=True,
                   state="STORING", reason=None)
    elif delta == 1:
        out.update(quantity=min(count_beam, count_weight), confidence="MOYENNE",
                   accepted=True, state="STORING",
                   reason="ecart de 1 entre barriere et pesee, quantite prudente retenue")
    else:
        out.update(quantity=0, confidence="NULLE", accepted=False,
                   state="QUARANTINE",
                   reason="ecart de comptage = %d (barriere %d / pesee %d)"
                          % (delta, count_beam, count_weight))
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

def fifo_allocate(boxes: list, ref: str, qty: int, now_sim: float,
                  order_id: str = "ORD-0") -> dict:
    """Answer the CDC's question: which box do I use first, and why not the others?

    `boxes` = every box currently in the warehouse (any ref, any state).
    Returns the contract shape documented in docs/contracts.md section 4.

    The REJECTED list is the deliverable. Anyone can sort a list by date; what
    convinces a jury is showing, per box, the reason it was passed over.
    """
    picks, rejected = [], []
    remaining = int(qty)

    candidates = [b for b in boxes if b["article_ref"] == ref]
    # deterministic FIFO key: oldest stored first, box_id breaks ties
    candidates.sort(key=lambda b: (b["t_in_sim"], b["box_id"]))

    for b in candidates:
        state = b["state"]

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

        if remaining <= 0:
            rejected.append({"box_id": b["box_id"], "reason": "plus recent (FIFO)",
                             "detail": "besoin deja couvert par des box plus anciens",
                             "t_in_sim": b["t_in_sim"]})
            continue

        take = min(avail, remaining)
        remaining -= take
        picks.append({"box_id": b["box_id"], "slot_id": b.get("slot_id"),
                      "take": take, "t_in_sim": b["t_in_sim"],
                      "rank": len(picks) + 1,
                      "partial": take < avail})

    return {
        "order_id": order_id,
        "ref": ref,
        "qty_requested": int(qty),
        "qty_allocated": int(qty) - remaining,
        "shortfall": remaining,
        "picks": picks,
        "rejected": rejected,
        "status": "PENDING" if picks else "IMPOSSIBLE",
    }


# ---------------------------------------------------------------------------
# 5. State machine
# ---------------------------------------------------------------------------

_TRANSITIONS = {
    "INCOMING":    {"IDENTIFYING", "QUARANTINE"},
    "IDENTIFYING": {"COUNTING", "QUARANTINE"},
    "COUNTING":    {"STORING", "QUARANTINE"},
    "STORING":     {"DRYING", "QUARANTINE"},
    "DRYING":      {"READY", "QUARANTINE"},
    "READY":       {"RESERVED", "QUARANTINE", "ARCHIVED"},
    "RESERVED":    {"PICKING", "READY"},          # READY = lock expired/cancelled
    "PICKING":     {"READY", "EMPTY"},            # READY = partial pick
    "EMPTY":       {"ARCHIVED"},
    "QUARANTINE":  {"ARCHIVED", "INCOMING"},      # INCOMING = operator re-presents it
    "ARCHIVED":    set(),
}


def can_transition(old: str, new: str) -> bool:
    return new in _TRANSITIONS.get(old, set())


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
    """Consume `take` cores. Partial picks keep t_in_sim — re-stamping breaks FIFO."""
    left = max(0, int(box["qty_available"]) - int(take))
    return {
        "qty_available": left,
        "state": "EMPTY" if left == 0 else "READY",
        "locked_by": None,
        "lock_expires_sim": None,
        # t_in_sim deliberately untouched
    }
