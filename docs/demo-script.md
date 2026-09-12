# The live demo — 7 minutes, on paper, rehearsed five times

Criterion 11 is worth 15 points and it is the only one you can lose while the
software works perfectly. Print this. Tape it to the table.

**P3 holds the keyboard. The best speaker talks. Never the same person.**

**Before anything else:** change `SESSION` in `backend/config.py` AND
`firmware/sketch.ino` from `nrw8` to something unique (e.g. `nrw8-team7`) —
the MQTT broker is public, and the two files must match exactly. Do this
once, at the venue, and commit it. Skipping this is the single most likely
way another team's traffic ends up inside this demo.

Before the jury walks up:

```
./run.sh                 # terminal 1
python tools/fake_device.py    # terminal 2 — only if Wokwi is not up
```

Then press **S** (load demo scenario) and **1** (iso camera). The rack must
never be empty when they arrive. Six boxes, 34 simulated hours, three cured.

---

## The seven beats

| # | Time | What you say | What you press |
|---|------|--------------|----------------|
| 1 | 0:40 | "A foundry core is sand, resin and catalyst. It must dry 24 hours before moulding. Today SOPAL tracks that zone by hand: plastic crates, no traceability. We turned it into a warehouse that sees, counts, remembers and decides." | nothing — let the 3D turn |
| 2 | 0:40 | "This is the real 6 by 6 by 6 metre room. Single-aisle stacker crane, 2 faces, 9 columns, 17 levels: **306 slots**, 93 % of the floor length and 80 % of the height." | **1** iso, then **4** top, back to **1** |
| 3 | 1:30 | "A box arrives. The plant model breaks a photoelectric barrier core by core and loads a scale. Those raw signals go over MQTT to the ESP32 — **the board is never told the answer**. It tares, debounces, counts, waits for the mass to settle, and reports." | **A** (box arrives) |
| 4 | 0:50 | "Two independent measurements: 37 by the barrier, 37 by mass ÷ the reference's known unit weight. They agree, so confidence HAUTE. The crane stores it and the clock starts — automatic timestamp, criterion 3." | point at the ESP32 pill and the new crate |
| 5 | 1:00 | "Now the anomaly. Same box, three cores missing from the mass." → *pick "Écart de comptage de 3"* → "Barrier says 30, scale says 27. Delta 2 or more is **quarantine**. It never enters stock. A wrongly labelled crate is caught the same way: the average core weight would not match the declared reference." | pick the anomaly, **A** |
| 6 | 1:30 | "Production needs 60 NY-114." → press **D** → "The system proposes BOX-1 then BOX-3 — oldest first. And here is the part that matters: **it tells you what it refused and why**. BOX-5 is rejected, not because it is newer, but because it still needs 9 hours of drying. FIFO you can audit." | **D**, then **C** to confirm — **confirm before doing anything else**: a reservation auto-releases after 2 simulated hours (`LOCK_TTL_H`), and pressing **J** in beat 7 jumps +6 h, which would expire an unconfirmed order in full view of the jury |
| 7 | 1:00 | *(placeholder — the cahier des charges never asked for a climate-adaptive cure time, and an earlier draft of this beat did; that idea was dropped, see docs/contracts.md CONTRACT VERSION 1.2. Criterion 10 (10 pts, "innovation") needs a replacement beat before the venue.)* One honest option that is already fully built and demonstrable: "The system tells you not just what it will do, but **exactly why it refused everything else** — the FIFO rejected list, per box, per reason — and a built-in consistency checker (`/db`, the green PASS badge) proves the database itself is never in an inconsistent state, live, on demand." | **J** (+6 h) to show a DRYING box crossing to READY; open `/db` to show the consistency badge |

Close: *"Everything you saw ran live. No video, no slides. The embedded board,
the warehouse logic and the 3D are three separate programs talking over MQTT
and WebSocket, and any one of them can be unplugged without the others lying."*

---

## Hotkeys (rehearse until your fingers know them)

```
A  box arrives        D  production demand     C  confirm the pick
J  +6 simulated hours S  load the demo scenario R  reset
1  iso   2  aisle   3  front   4  top
```

---

## Fallback drill — practise switching MID-SENTENCE

| Level | Trigger | What you do | What the jury sees |
|-------|---------|-------------|--------------------|
| **L0** | normal | Wokwi ESP32 is subscribed and answering | "ESP32 online" pill green |
| **L1** | Wokwi died, or venue wifi ate MQTT | nothing — press **A** as usual; after ~2.5 s the backend does the board's arithmetic itself and stores the box | pill goes amber, "Mode L1", **the screen is otherwise identical** |
| **L1b** | you want a live board without Wokwi | `python tools/fake_device.py` in a spare terminal | pill green again, same payloads |
| **L2** | backend crashed | restart `./run.sh`, press **S** | the scenario rebuilds in 2 seconds |

The dashboard also falls back from WebSocket to polling on its own. You will
not notice; neither will they.

---

## Hostile questions, and the honest answer

**"Why no camera? The brief says *voir*."**
> A photoelectric barrier *is* optical sensing, and it is what foundries
> actually use — sand dust and variable lighting are exactly where a camera
> fails. The operator still sees the zone: through the 3D twin, in real time.
> And we do not rely on one sense — the scale is an independent second opinion
> on the same box, which a camera alone cannot give you.

**"Dividing weight by unit mass is fragile — cores vary."**
> In this system the unit mass per reference is given data, not measured
> output, so the division is exact. And we never trust it alone: it only ever
> *cross-checks* the barrier count. Where they disagree by 2 or more, we refuse
> the box rather than guess.

**"Your rack has no physical FIFO mechanism."**
> Deliberately. Gravity-flow lanes force FIFO but need two access faces, which
> a single-aisle crane cannot serve. We kept single-deep direct addressing —
> any box reachable in one move — and enforce FIFO in software, where it is
> auditable. The 3° incline is for repeatable seating against a cushioned
> stop, not for sequencing.

**"What happens if two orders want the same box?"**
> `RESERVED` is a real lock with an expiry. The second order sees it as
> rejected with the reason "reserved, ORD-1", and the lock releases itself if
> the first order is never confirmed.

**"Is the 3D just a picture?"**
> It is driven by the same WebSocket as the table. Watch the slot colour change
> when the box cures. Nothing in that scene is animated by hand.
