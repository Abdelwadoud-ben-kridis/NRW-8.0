"""
tools/test_firmware_contract.py — static parity check between
firmware/sketch.ino (C++, compiled by Wokwi) and backend/config.py (Python).

    python tools/test_firmware_contract.py

The two files independently declare the same numbers (scale calibration,
session, broker) because the firmware has no way to import backend/config.py
at compile time. Nothing else stops them from silently drifting apart if one
is edited without the other -- this script parses both source files as text
and fails loudly the moment they disagree, instead of only finding out live
at the venue when a box's mass reads wrong. It also pins the wiring
(firmware/diagram.json) and the box_done payload shape to the judging
requirements, so an edit that quietly drops a pin or a field is caught here
rather than during the demo.

No server, no DB, no Arduino toolchain needed: this only reads text.
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import config as C  # noqa: E402  (path insert must come first)

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


sketch = open(os.path.join(ROOT, "firmware", "sketch.ino"), encoding="utf-8").read()
diagram = json.load(open(os.path.join(ROOT, "firmware", "diagram.json"), encoding="utf-8"))


def find(pattern, text, label):
    m = re.search(pattern, text)
    if not m:
        check("found %s in sketch.ino" % label, False, pattern)
        return None
    return m.group(1)


# --- 1. scale calibration: the actual reason a wrong count would ship ------
num = find(r"G_PER_MV\s*=\s*([\d.]+)f?\s*/", sketch, "G_PER_MV numerator")
den = find(r"G_PER_MV\s*=\s*[\d.]+f?\s*/\s*([\d.]+)f?\s*;", sketch, "G_PER_MV denominator")
if num and den:
    fw_g_per_mv = float(num) / float(den)
    check("firmware G_PER_MV (%.4f) == backend G_PER_MV (%.4f)"
          % (fw_g_per_mv, C.G_PER_MV),
          abs(fw_g_per_mv - C.G_PER_MV) < 1e-6)

# --- 2. session / broker: a mismatch here means the board never joins ------
fw_session = find(r'#define\s+SESSION\s+"([^"]+)"', sketch, "SESSION")
fw_host = find(r'#define\s+MQTT_HOST\s+"([^"]+)"', sketch, "MQTT_HOST")
fw_port = find(r"#define\s+MQTT_PORT\s+(\d+)", sketch, "MQTT_PORT")

cfg_src = open(os.path.join(ROOT, "backend", "config.py"), encoding="utf-8").read()
cfg_session = re.search(r'SCW_SESSION",\s*"([^"]+)"', cfg_src).group(1)
cfg_host = re.search(r'SCW_MQTT_HOST",\s*"([^"]+)"', cfg_src).group(1)
cfg_port = re.search(r'SCW_MQTT_PORT",\s*"(\d+)"', cfg_src).group(1)

if fw_session:
    check("firmware SESSION default (%s) == backend default (%s)"
          % (fw_session, cfg_session), fw_session == cfg_session)
if fw_host:
    check("firmware MQTT_HOST default (%s) == backend default (%s)"
          % (fw_host, cfg_host), fw_host == cfg_host)
if fw_port:
    check("firmware MQTT_PORT default (%s) == backend default (%s)"
          % (fw_port, cfg_port), fw_port == cfg_port)

# --- 3. box_done must carry every field the contract requires --------------
m = re.search(r"void publishBoxDone\(\)\s*\{(.*?)\n\}", sketch, re.S)
body = m.group(1) if m else ""
check("publishBoxDone() found in sketch.ino", bool(m))
for field in ("ref", "count_beam", "gross_g", "t_c", "rh", "fw"):
    check('box_done includes "%s"' % field, ('d["%s"]' % field) in body)

# --- 4. wiring matches the judging requirement (real, legible pins) --------
required_pins = {"PIN_BEAM": "25", "PIN_DONE": "26", "PIN_POT": "34",
                 "PIN_LED": "2", "PIN_DHT": "15"}
for name, pin in required_pins.items():
    got = find(r"#define\s+%s\s+(\d+)" % name, sketch, name)
    if got:
        check("%s is GPIO %s" % (name, pin), got == pin)

conns = diagram["connections"]


def wired(a_suffix, b_suffix):
    for c in conns:
        a, b = c[0], c[1]
        if (a.endswith(a_suffix) and b.endswith(b_suffix)) or \
           (a.endswith(b_suffix) and b.endswith(a_suffix)):
            return True
    return False


check("diagram.json: beam button on esp:D25", wired("btnBeam:2.l", "esp:D25"))
check("diagram.json: done button on esp:D26", wired("btnDone:2.l", "esp:D26"))
check("diagram.json: potentiometer signal on esp:D34", wired("pot:SIG", "esp:D34"))
check("diagram.json: DHT22 data on esp:D15", wired("dht:SDA", "esp:D15"))
check("diagram.json: LED driven from esp:D2 through the resistor",
      wired("esp:D2", "r1:1"))
r1 = next((p for p in diagram["parts"] if p["id"] == "r1"), None)
check("diagram.json: LED series resistor is 220 ohm",
      bool(r1) and r1["attrs"].get("value") == "220")

print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
