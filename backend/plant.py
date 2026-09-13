"""The PLANT MODEL — the pretend physical world.

It produces TWO raw signals, from two independent simulated sensors, and
never an answer:

  - a load-cell voltage script that climbs as cores are loaded and then
    settles (`build_arrival`), fed to the ESP32 exactly as before;
  - one simulated VISION-STATION reading (`simulate_vision`) -- a shape
    signature and a visible-core count, taken once per arrival, fed
    straight to `algo.engine.identify_core`/`assess_box` in
    `backend/warehouse.py::create_box`. This is the CDC's "voir": a second,
    independent sensor the barcode is checked AGAINST, not a decision made
    by either sensor alone.

Neither sensor is ever told the answer. Identification is a three-way
agreement (barcode declares, vision confirms the shape, the scale confirms
the weight); quantity is the ESP32's job to weigh, cross-checked against
what the vision station counted. Feeding raw signals instead of answers is
what makes criteria 2/3/9 defensible under questioning.
"""
from __future__ import annotations

import random

from algo.engine import CORE_MASS_CV
from backend import config as C

ANOMALIES = {
    "none": "Arrivee nominale",
    "mismatch": "Le contenu reel ne correspond pas au code-barre scanne",
    "empty": "Caisse vide deposee sur le convoyeur",
}


def pick_swap_article(article: dict, other_articles: list[dict] | None) -> dict | None:
    """Which OTHER real reference physically ends up in a "mismatch" crate.

    Deterministic (alphabetically next `ref` in the catalogue, excluding
    `article` itself) so a rehearsed demo shows the same swap twice, and
    shared by `build_arrival` and `simulate_vision` so the weight and the
    simulated camera agree on which wrong reference is actually in the box
    -- a real misfiled crate would be wrong on BOTH sensors at once, not
    just optically. Returns None if there is no other reference to swap in
    (a catalogue of one), in which case the caller falls back to the old
    same-reference-scaled-mass anomaly.
    """
    if not other_articles:
        return None
    candidates = [a for a in other_articles if a["ref"] != article["ref"]]
    if not candidates:
        return None
    return sorted(candidates, key=lambda a: a["ref"])[0]


def build_arrival(barcode_unit_mass_g: float, qty: int, anomaly: str = "none",
                  noise_mv: float = 0.6, core_mass_cv: float = CORE_MASS_CV,
                  swap_unit_mass_g: float | None = None) -> list[dict]:
    """Return the full raw-signal script for one box arriving on the conveyor.

    Each frame is exactly the payload of scw/<S>/sim/raw, minus t_sim which
    the caller stamps. Played back at ~10 Hz it looks and behaves like a
    real counting station: mass steps up core by core, then settles. The
    LAST frame carries `"final": true` -- a limit-switch-style "the crate has
    reached the end of the counting station" signal, not a measurement, so
    the firmware can stop waiting out its stability timer the instant the
    conveyor itself says the box is done (contract 1.7 finding: at the
    original 1.2 s timeout against a 1.4 s settle window, ordinary MQTT
    jitter could end the box a fraction of a second early and clip the last
    core -- see firmware/sketch.ino).

    `barcode_unit_mass_g` is what the SCANNED barcode registered for this
    box (backend/warehouse.py::register_barcode) -- the "truth" the ESP32's
    weight reading will be judged against, one layer up, in
    algo/engine.py::assess_box. `qty` is the simulator's own hidden ground
    truth for how many cores are physically going onto the scale; the
    system never sees it directly, only the resulting mass. Each core's own
    contribution to that mass is jittered by `core_mass_cv` (real cores are
    never bit-for-bit identical) -- this is what makes the quantity-check
    honest to test against, instead of every clean box landing on an exact
    multiple of the registered weight by construction.

    `swap_unit_mass_g` (from `pick_swap_article`, passed by the caller so it
    agrees with what `simulate_vision` shows for the same anomaly) is the
    REAL per-noyau weight for a "mismatch" arrival -- the physical cores
    are a different, heavier-or-lighter reference than the barcode
    declares, not just the declared reference scaled by a fudge factor.
    Falling back to `barcode_unit_mass_g` unchanged when no swap reference
    exists keeps this callable on a catalogue of one.
    """
    real_unit = (swap_unit_mass_g if (anomaly == "mismatch" and swap_unit_mass_g)
                else barcode_unit_mass_g)
    real_qty = qty

    if anomaly == "empty":
        real_qty = 0

    frames: list[dict] = []
    mv = 0.0

    def push(mv_val, n=1, final=False):
        for i in range(n):
            frame = {"load_mv": max(0, int(round(
                mv_val + random.uniform(-noise_mv, noise_mv))))}
            if final and i == n - 1:
                frame["final"] = True
            frames.append(frame)

    # 1. empty crate seated on the scale -> this is what the ESP32 tares against
    mv = C.TARE_G / C.G_PER_MV
    push(mv, 8)

    # 2. cores loaded in, mass stepping up core by core -- each core's own
    # mass varies around the registered value, same as a real casting batch
    for _ in range(real_qty):
        core_mass = real_unit * (1.0 + random.gauss(0.0, core_mass_cv))
        mv += core_mass / C.G_PER_MV
        push(mv, 2)

    # 3. mass settles, ESP32 waits for stability before declaring the box
    # done -- the very last frame marks the end of the counting station.
    push(mv, 14, final=True)
    return frames


def final_gross_g(frames: list[dict]) -> float:
    """What the scale reads at the end — used by the L1 fallback path."""
    return frames[-1]["load_mv"] * C.G_PER_MV if frames else 0.0


# Nominal foundry-core shapes for the simulated vision station, keyed by what
# a fresh custom reference gets by default (backend/db.py::add_article) if it
# has no measured shape of its own -- generic enough not to collide with any
# of the four seeded references' own signatures.
_GENERIC_SHAPE = {"len_mm": 100.0, "wid_mm": 70.0, "h_mm": 40.0, "holes": 2}


def simulate_vision(article: dict, qty: int, anomaly: str = "none",
                    other_articles: list[dict] | None = None,
                    shape_noise: float = 0.03) -> dict:
    """One simulated camera-station reading, taken once per arrival --
    independent of the load cell, and never told the barcode's answer.

    `article` is what the SCANNED barcode declares this box should contain.
    For `anomaly == "mismatch"` the camera instead sees a DIFFERENT real
    reference's shape (physically wrong cores were loaded into this labelled
    crate) -- picked deterministically (alphabetically next `ref` in the
    catalogue) so a rehearsed demo shows the same thing twice. This is what
    lets `algo.engine.identify_core` catch a mismatch on sight, regardless
    of whether the weight alone happens to land on a clean multiple of the
    wrong reference's mass for this particular quantity (finding: weight
    alone missed 1 case in 4).

    `count_visible` is the second, independent quantity measurement
    (criterion 3): the true count with a little occlusion noise, since a
    camera rarely sees every core in a crate at a single glance.
    """
    src = article
    if anomaly == "mismatch":
        swap = pick_swap_article(article, other_articles)
        if swap:
            src = swap

    def jitter(v):
        return max(0.0, v * (1.0 + random.uniform(-shape_noise, shape_noise)))

    real_qty = 0 if anomaly == "empty" else qty
    seen = real_qty
    if real_qty > 0:
        occlusion = int(round(random.gauss(0, max(0.4, real_qty * 0.015))))
        seen = max(0, real_qty - max(0, occlusion))

    shape = src or _GENERIC_SHAPE
    return {
        "len_mm": round(jitter(shape.get("len_mm") or _GENERIC_SHAPE["len_mm"]), 1),
        "wid_mm": round(jitter(shape.get("wid_mm") or _GENERIC_SHAPE["wid_mm"]), 1),
        "h_mm": round(jitter(shape.get("h_mm") or _GENERIC_SHAPE["h_mm"]), 1),
        "holes": int(shape.get("holes") if shape.get("holes") is not None
                    else _GENERIC_SHAPE["holes"]),
        "count_visible": seen,
    }
