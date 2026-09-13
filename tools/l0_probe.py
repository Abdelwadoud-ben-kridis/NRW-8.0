"""
tools/l0_probe.py — proves the LIVE DEVICE path (L0), not just L1 fallback.

    python tools/l0_probe.py            (backend must be running, default
                                          :8000, reachable on the real MQTT
                                          broker)

This calls /api/reset -- point it at an isolated instance (SCW_DB/
SCW_SESSION on that server), matching SCW_SESSION/SCW_BASE here (both are
also inherited by the tools/fake_device.py subprocess this launches):
    SCW_SESSION=nrw8-test SCW_BASE=http://localhost:8793 python tools/l0_probe.py

tools/smoke.py and tools/mqtt_probe.py both run with no device attached, so
every arrival they trigger resolves through the L1 backend fallback. That
never proves the thing criterion 9 is actually judged on: a live device
answering on scw/<SESSION>/dev/box_done and the backend accepting its
verdict as-is (mode "L0").

This probe launches tools/fake_device.py -- the repo's own byte-identical
stand-in for the ESP32 (see its docstring) -- as a subprocess, then drives
one arrival per anomaly through /api/sim/arrival and checks:
  - the arrival resolved in mode "L0" (the device answered in time)
  - exactly one box was created per arrival
  - the device sent exactly one box_done per arrival: no box_done_ignored
    event at all (contract 1.10 -- the sketch used to send two per box, which
    the backend dedup silently swallowed)
  - the box landed in the state its anomaly is supposed to produce

none/mismatch/empty are scale-reading outcomes the engine is supposed to
catch by the numbers, independent of which device answered -- this probe
exists to prove the LIVE device path reaches the same verdict as L1 already
does, not to re-derive the anomaly logic itself (algo/test_engine.py owns
that).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("SCW_BASE", "http://localhost:8000")
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
    # Wait until the backend actually receives the device's telemetry: paho
    # drops publishes until connected, and on_connect subscribes first, so
    # telemetry seen => subscribed. A fixed sleep raced a slow public broker:
    # the device missed start_box, weighed the frames under its default ref
    # and sent an unsolicited box_done, pushing the arrival itself to L1.
    deadline = time.monotonic() + 30.0
    while not call("/api/state")["device"].get("online"):
        if time.monotonic() > deadline:
            print("  FAIL fake_device never came online on the broker")
            sys.exit(1)
        time.sleep(0.5)
    # Telemetry proves the connection, not that the cmd subscription is live
    # (a start_box it misses makes it weigh under its default ref). Warm up
    # with throwaway arrivals until one resolves L0, then start clean.
    for _ in range(4):
        time.sleep(1.0)
        call("/api/reset", {})
        if call("/api/sim/arrival", {"ref": "NY-114", "qty": 30}).get("mode") == "L0":
            break
    else:
        print("  FAIL fake_device never answered a warm-up arrival in L0")
        sys.exit(1)

    call("/api/reset", {})

    CASES = [
        # (anomaly, expect_mode, expect_state)
        ("none", "L0", "DRYING"),
        ("mismatch", "L0", "QUARANTINE"),
        ("empty", "L0", "QUARANTINE"),
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

    ignored = [e for e in call("/api/events?limit=200") if e["kind"] == "box_done_ignored"]
    check("device sent exactly one box_done per arrival (no box_done_ignored)",
          not ignored, [e["payload"] for e in ignored])

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
