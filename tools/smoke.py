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
call("/api/demand/cancel", {"order_id": plan["order_id"]})

# 3. jump past the cure
call("/api/clock", {"jump_h": 26})
time.sleep(0.6)
st = call("/api/state")
check("boxes cured on their own",
      sum(1 for b in st["boxes"] if b["state"] == "READY") == 2)

# 4. FIFO: oldest first, across two boxes, with the newest correctly rejected
plan = call("/api/demand", {"ref": "NY-114", "qty": 30})
check("allocated 30", plan["qty_allocated"] == 30, plan["qty_allocated"])
check("oldest box first", plan["picks"][0]["box_id"] == "BOX-1",
      [p["box_id"] for p in plan["picks"]])
check("spans two boxes", len(plan["picks"]) == 2)
check("takes 22 then 8", [p["take"] for p in plan["picks"]] == [22, 8],
      [p["take"] for p in plan["picks"]])

st = call("/api/state")
check("picked boxes are RESERVED",
      sum(1 for b in st["boxes"] if b["state"] == "RESERVED") == 2)

# 5. confirm -> partial pick keeps t_in_sim, full pick empties the slot
before = {b["box_id"]: b["t_in_sim"] for b in st["boxes"]}
call("/api/demand/confirm", {"order_id": plan["order_id"]})
st = call("/api/state")
b2 = [b for b in st["boxes"] if b["box_id"] == "BOX-2"][0]
check("partial pick keeps t_in_sim", b2["t_in_sim"] == before["BOX-2"])
check("partial box back to READY", b2["state"] == "READY", b2["state"])
check("partial qty = 10", b2["qty_available"] == 10, b2["qty_available"])
b1 = [b for b in st["boxes"] if b["box_id"] == "BOX-1"][0]
check("emptied box released its slot", b1["state"] == "EMPTY" and not b1["slot_id"])

# 6. quarantine path
call("/api/reset", {})
art = [a for a in call("/api/articles") if a["ref"] == "NY-114"][0]
call("/api/sim/env", {"t_c": 24.0, "rh": 52.0})
res = call("/api/sim/arrival", {"ref": "NY-114", "qty": 30, "anomaly": "delta"})
st = call("/api/state")
check("anomaly box quarantined",
      any(b["state"] == "QUARANTINE" for b in st["boxes"]),
      [b["state"] for b in st["boxes"]])

# 7. innovation: a cold wet room extends the cure beyond 24 h
call("/api/reset", {})
call("/api/sim/env", {"t_c": 15.0, "rh": 85.0})
call("/api/sim/box", {"ref": "NY-220", "qty": 20})
st = call("/api/state")
check("adaptive cure extended", st["boxes"][0]["required_cure_h"] > 24.0,
      st["boxes"][0]["required_cure_h"])

# 8. events exist (this is what L2 replay reads)
check("event log populated", len(call("/api/events?limit=50")) > 0)

print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
