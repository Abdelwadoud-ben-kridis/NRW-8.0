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
| 3 | 1:30 | "Before a box ever reaches the conveyor, a worker labels it and registers what it holds — a reference and that specific box's own per-noyau weight — right here in the dashboard. No one counts anything by hand; that registration is the only human input in the whole chain. The box arrives, its scanner reads the barcode back — a lookup, not a guess — then it loads onto the scale AND passes a vision station: two independent sensors on the way in. That raw weight goes over MQTT to the ESP32 — **the board is never told the answer**, it just tares, waits for the mass to settle, and reports. Watch its own OLED — that's the board's own view of the same weighing, live." | register a barcode, then **A** (box arrives) — point at the Wokwi OLED as it moves TARE → WEIGHING → STABLE |
| 4 | 0:50 | "Net weight ÷ that barcode's own registered per-noyau weight = 37, a clean division, and the vision station's own shape reading confirms it really is an NY-114 — confidence HAUTE on both identity and count. The crane stores it and the clock starts — automatic timestamp, criterion 3." | point at the ESP32 pill, the vision-id readout, the new crate, and its barcode in the inventory row |
| 5 | 1:00 | "Now the anomaly. Same barcode, but the physical cores don't match what it promised — swapped after labelling." → *pick "mismatch"* → "Watch: even when the weight alone could coincidentally look clean for some quantities, the camera sees a shape that doesn't match this barcode's declared reference, so it's **quarantined** immediately. It never enters stock." | pick the anomaly, **A** |
| 6 | 1:30 | "Production needs 60 NY-114." → press **D** → "The system proposes BOX-1, taking the rest it needs from BOX-3 — oldest first, and only what's needed: the leftover cores stay in stock, still first in line next time. And here is the part that matters: **it tells you what it refused and why**. BOX-5 is rejected, not because it is newer, but because it still needs 9 hours of drying — and if the pipeline can't cover it at all, the panel names exactly when it will." | **D**, then **C** to confirm — **confirm before doing anything else**: a reservation auto-releases after 2 simulated hours (`LOCK_TTL_H`), and pressing **J** in beat 7 jumps +6 h, which would expire an unconfirmed order in full view of the jury |
| 7 | 1:00 | "Three ideas for criterion 10. First: the system tells you not just what it will do, but **exactly why it refused everything else** — the FIFO rejected list, per box, per reason — and a built-in consistency checker (`/db`, the green PASS badge) proves the database itself is never in an inconsistent state, live, on demand. Second: when a demand can't be covered by existing stock at all — not just curing, genuinely absent — the system doesn't just refuse it. It opens a production batch, tracks it, and **ships itself automatically** the moment the last box finishes curing — no partial shipments, no manual bookkeeping. Third: quarantine isn't a dead end. A box flagged over a bad weighing can be **re-weighed on the spot** and re-enters the cure cycle the moment the numbers check out, instead of sitting there forever." | **J** (+6 h) to show a DRYING box crossing to READY (watch the banner); open `/db` to show the consistency badge; press **D** for a reference with zero stock to show a batch open — it ships itself on the next **J**, no button needed; open the quarantine panel and re-weigh a flagged box |

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

**"Why a barcode at all? The brief says *voir*."**
> Both are simulated, and they check each other. A barcode scan is exact,
> unlike a camera guessing at a part under sand dust and variable lighting
> — but a barcode only says what a box CLAIMS to hold, so a simulated
> vision station reads its actual shape and cross-checks it against that
> claim. Neither is trusted blind: if vision disagrees with the barcode, or
> the weight doesn't line up with a clean count either sensor supports, the
> box is quarantined rather than guessed at. The operator also sees the
> zone through the 3D twin, in real time.

**"Dividing weight by unit mass is fragile — cores vary."**
> It used to be — a fixed tolerance either missed real mismatches for some
> quantities or wrongly quarantined honest boxes at realistic per-core
> variance. That's why there are now two independent measurements: the
> per-noyau weight registered on the SPECIFIC physical box's own barcode
> (not a shared article average), and the vision station's own visible-core
> count. When they agree, or are off by at most one or two, the box is
> accepted at the more conservative figure; a bigger gap, or vision naming
> a different reference entirely, quarantines it rather than guessing.

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

**"What if the scanner misreads or can't read the barcode at all?"**
> Then it fails to look anything up, and the box is quarantined as an
> unregistered code — visibly, in the event log, naming the exact code it
> read. It is never silently accepted on a best guess. The same goes for a
> barcode used twice: the second scan is refused as already consumed, so one
> sticker can never be replayed onto two different physical boxes.

**"What if there's simply no stock at all for what's being asked?"**
> Then it isn't a refusal any more — it's a production batch. The system
> tracks exactly what's been produced for it and what's still curing, and
> ships the whole batch together the instant the last box crosses 24 h. If
> a box in that batch gets quarantined along the way, the rest still ships
> — short, and flagged as short — rather than blocking forever on a
> replacement nobody asked for.

**"Doesn't that batch path let production skip the FIFO/audit story?"**
> No — it only ever fires when existing stock, curing or not, genuinely
> can't cover the request. Any demand that CAN be met from what's already
> in the warehouse still goes through the exact same FIFO, oldest-box-first,
> full-audit-trail path as always. The batch path is strictly the
> make-to-order fallback, never a shortcut around it.
