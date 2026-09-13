"""
tools/l0_probe.py — proves the LIVE DEVICE path (L0), not just L1 fallback.

    python tools/l0_probe.py            (backend must be running on :8000,
                                          reachable on the real MQTT broker)

tools/smoke.py and tools/mqtt_probe.py both run with no device attached, so
every arrival they trigger resolves through the L1 backend fallback. That
never proves the thing criterion 9 is actually judged on: a live device
answering on scw/<SESSION>/dev/box_done and the backend accepting its
verdict as-is (mode "L0").

This probe launches tools/fake_device.py -- the repo's own byte-identical
stand-in for the ESP32 (see its docstring) -- as a subprocess, then drives
one arrival per anomaly through /api/sim/arrival and checks:
  - the arrival resolved in mode "L0" (the device answered in time), except
    sensor_dead, where a real board legitimately never sees a beam edge
    either and L1 is the CORRECT, expected outcome, not a failure
  - exactly one box was created per arrival (no duplicate box_done)
  - the box landed in the state its anomaly is supposed to produce

Nominal/off_by_one/mislabel/sensor_dead are article-mass mismatches the
engine is supposed to catch by the numbers, independent of which device
answered -- this probe exists to prove the LIVE device path reaches the same
verdict as L1 already does, not to re-derive the anomaly logic itself
(algo/test_engine.py owns that).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://localhost:8000"
fails = []


def call(path, body=None, timeout=40):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


print("l0 probe against", BASE)
dev = subprocess.Popen([sys.executable, os.path.join("tools", "fake_device.py")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(2.0)   # let it connect and subscribe before the first arrival

    call("/api/reset", {})
    call("/api/sim/env", {"t_c": 24.0, "rh": 45.0})

    CASES = [
        # (anomaly, expect_mode, expect_state)
        ("none", "L0", "DRYING"),
        ("off_by_one", "L0", "DRYING"),
        ("delta", "L0", "QUARANTINE"),
        ("mislabel", "L0", "QUARANTINE"),
        # a dead beam sensor means a REAL board never sees an edge either --
        # L1 is the correct outcome here, not a device failure.
        ("sensor_dead", "L1", "QUARANTINE"),
    ]

    for anomaly, expect_mode, expect_state in CASES:
        before = call("/api/state")["boxes"]
        res = call("/api/sim/arrival", {"ref": "NY-114", "qty": 30, "anomaly": anomaly})
        after = call("/api/state")["boxes"]
        check("%-12s mode == %s" % (anomaly, expect_mode),
              res.get("mode") == expect_mode, res.get("mode"))
        check("%-12s exactly one box created" % anomaly,
              len(after) == len(before) + 1, (len(before), len(after)))
        new_boxes = [b for b in after if b["box_id"] not in {b["box_id"] for b in before}]
        state = new_boxes[0]["state"] if new_boxes else None
        check("%-12s box state == %s" % (anomaly, expect_state), state == expect_state, state)

    chk = call("/api/db/check")
    check("consistency checker: overall PASS after probe", chk["overall"] == "PASS",
          [c for c in chk["checks"] if c["severity"] != "PASS"])
finally:
    dev.terminate()
    try:
        dev.wait(timeout=5)
    except subprocess.TimeoutExpired:
        dev.kill()

print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
