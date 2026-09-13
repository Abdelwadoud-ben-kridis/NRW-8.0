"""
tools/smoke.py — 60-second proof that the whole backend still works.

    python tools/smoke.py            (backend must be running on :8000)

Run it after every merge, and once more at H23 before the feature freeze.
It exercises the exact path the jury will watch: a box arrives, cures, gets
proposed by FIFO, and gets picked.
"""
import json
import sys
import time
import urllib.request

BASE = "http://localhost:8000"
fails = []


def call(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


print("smoke test against", BASE)
call("/api/reset", {})
call("/api/clock", {"speed": 0})
call("/api/sim/env", {"t_c": 24.0, "rh": 45.0})   # -> cure is exactly the 24 h floor

# 1. two boxes of the same ref arrive 5 simulated hours apart
call("/api/sim/box", {"ref": "NY-114", "qty": 22})
call("/api/clock", {"jump_h": 5})
call("/api/sim/box", {"ref": "NY-114", "qty": 18})
st = call("/api/state")
check("two boxes stored", len(st["boxes"]) == 2, len(st["boxes"]))
check("both are DRYING", all(b["state"] == "DRYING" for b in st["boxes"]))
check("slots assigned", all(b["slot_id"] for b in st["boxes"]))
check("cure == 24 h floor", all(abs(b["required_cure_h"] - 24.0) < 0.01 for b in st["boxes"]))

# 2. demand before cure -> nothing allocated, reasons given
plan = call("/api/demand", {"ref": "NY-114", "qty": 30})
check("uncured demand refused", plan["qty_allocated"] == 0, plan["status"])
check("rejections explained", len(plan["rejected"]) == 2,
      [r["reason"] for r in plan["rejected"]])
# status is IMPOSSIBLE (no picks) -- nothing was reserved, so there is
# nothing to cancel; cancelling an IMPOSSIBLE order is refused by design
# (backend/warehouse.py::cancel, finding F5) and would 409 here.

# 3. jump past the cure
call("/api/clock", {"jump_h": 26})
time.sleep(0.6)
st = call("/api/state")
check("boxes cured on their own",
      sum(1 for b in st["boxes"] if b["state"] == "READY") == 2)

# 4. FIFO: oldest first, across two boxes. A box is never split (contract
#    1.3), so covering 30 needs both whole boxes -- 22 + 18 = 40, rounding
#    up rather than splitting BOX-2 to hand out exactly 30.
plan = call("/api/demand", {"ref": "NY-114", "qty": 30})
check("rounds up to 40 (never splits a box)", plan["qty_allocated"] == 40, plan["qty_allocated"])
check("oldest box first", plan["picks"][0]["box_id"] == "BOX-1",
      [p["box_id"] for p in plan["picks"]])
check("spans two boxes", len(plan["picks"]) == 2)
check("takes whole boxes 22 then 18", [p["take"] for p in plan["picks"]] == [22, 18],
      [p["take"] for p in plan["picks"]])

st = call("/api/state")
check("picked boxes are RESERVED",
      sum(1 for b in st["boxes"] if b["state"] == "RESERVED") == 2)

# 5. confirm -> both boxes are taken whole, so both empty and release their slot
call("/api/demand/confirm", {"order_id": plan["order_id"]})
st = call("/api/state")
b1 = [b for b in st["boxes"] if b["box_id"] == "BOX-1"][0]
b2 = [b for b in st["boxes"] if b["box_id"] == "BOX-2"][0]
check("BOX-1 emptied and released its slot", b1["state"] == "EMPTY" and not b1["slot_id"])
check("BOX-2 emptied and released its slot", b2["state"] == "EMPTY" and not b2["slot_id"])

# 6. quarantine path
call("/api/reset", {})
art = [a for a in call("/api/articles") if a["ref"] == "NY-114"][0]
call("/api/sim/env", {"t_c": 24.0, "rh": 52.0})
res = call("/api/sim/arrival", {"ref": "NY-114", "qty": 30, "anomaly": "delta"})
st = call("/api/state")
check("anomaly box quarantined",
      any(b["state"] == "QUARANTINE" for b in st["boxes"]),
      [b["state"] for b in st["boxes"]])

# 7. contract 1.2: cure is FIXED at 24 h regardless of climate (no adaptive
# model -- the CDC never asked for one; T/RH are evidence-only now)
call("/api/reset", {})
call("/api/sim/env", {"t_c": 15.0, "rh": 85.0})   # a cold, humid room
call("/api/sim/box", {"ref": "NY-220", "qty": 20})
st = call("/api/state")
check("cure is exactly 24h even in a cold/humid room",
      abs(st["boxes"][0]["required_cure_h"] - 24.0) < 0.01,
      st["boxes"][0]["required_cure_h"])

# 8. events exist (this is what L2 replay reads)
check("event log populated", len(call("/api/events?limit=50")) > 0)

# 9. double confirm deducts exactly once (finding F1)
call("/api/reset", {})
call("/api/sim/env", {"t_c": 24.0, "rh": 45.0})
call("/api/sim/box", {"ref": "NY-114", "qty": 40})
call("/api/clock", {"jump_h": 25})
time.sleep(0.4)
plan = call("/api/demand", {"ref": "NY-114", "qty": 10})
r1 = call("/api/demand/confirm", {"order_id": plan["order_id"]})
r2 = call("/api/demand/confirm", {"order_id": plan["order_id"]})
check("first confirm applies", r1.get("already") is False, r1)
check("second confirm is a no-op, not an error", r2.get("already") is True, r2)
st = call("/api/state")
box1 = [b for b in st["boxes"] if b["box_id"] == "BOX-1"][0]
# the whole 40-core box is taken (never split, contract 1.3) -- what this
# test is really proving is that the SECOND confirm doesn't deduct again
check("qty deducted exactly once", box1["qty_available"] == 0, box1["qty_available"])

# confirming a cancelled order is refused, not silently applied
call("/api/reset", {})
call("/api/sim/env", {"t_c": 24.0, "rh": 45.0})
call("/api/sim/box", {"ref": "NY-114", "qty": 40})
call("/api/clock", {"jump_h": 25})
time.sleep(0.4)
plan = call("/api/demand", {"ref": "NY-114", "qty": 10})
call("/api/demand/cancel", {"order_id": plan["order_id"]})
try:
    call("/api/demand/confirm", {"order_id": plan["order_id"]})
    check("confirming a cancelled order is refused (HTTP error expected)", False)
except Exception:
    check("confirming a cancelled order is refused (HTTP error expected)", True)

# 10. reservation expiry cancels the ORDER too, not just the box lock
# (finding F4) -- LOCK_TTL_H (backend/config.py) is 2.0 sim-h
call("/api/reset", {})
call("/api/sim/env", {"t_c": 24.0, "rh": 45.0})
call("/api/sim/box", {"ref": "NY-114", "qty": 40})
call("/api/clock", {"jump_h": 25})
time.sleep(0.4)
plan = call("/api/demand", {"ref": "NY-114", "qty": 10})
call("/api/clock", {"jump_h": 3})       # past the 2h lock TTL
time.sleep(0.6)                          # let loop_clock's sweep run
st = call("/api/state")
box1 = [b for b in st["boxes"] if b["box_id"] == "BOX-1"][0]
check("expired reservation releases the box back to READY",
      box1["state"] == "READY", box1["state"])
check("expired order no longer appears as pending",
      not any(o["order_id"] == plan["order_id"] for o in st.get("orders_pending", [])))
ev = call("/api/events?limit=10")
check("an order_expired event was logged",
      any(e["kind"] == "order_expired" for e in ev), [e["kind"] for e in ev])

# 11. unknown reference is quarantined WITHOUT polluting a real article
# (finding F7 -- it used to be misfiled under NY-114)
call("/api/reset", {})
call("/api/sim/box", {"ref": "NOPE-999", "qty": 10})
st = call("/api/state")
bad = [b for b in st["boxes"] if b["state"] == "QUARANTINE"]
check("unknown ref quarantined", len(bad) == 1, bad)
check("does not pollute NY-114's article", bad[0]["ref"] is None, bad[0]["ref"])

# 12. the read-only consistency checker (backend/consistency.py) is green
chk = call("/api/db/check")
check("consistency checker: overall PASS", chk["overall"] == "PASS",
      [c for c in chk["checks"] if c["severity"] != "PASS"])

print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
