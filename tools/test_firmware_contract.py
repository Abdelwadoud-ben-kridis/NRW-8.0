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
# count_beam is gone from the wire payload (contract 1.7): weight is the
# board's only sensor, the second count comes from the simulated vision
# station upstream, not from anything this board reads.
for field in ("ref", "gross_g", "fw"):
    check('box_done includes "%s"' % field, ('d["%s"]' % field) in body)
check("count_beam is NOT sent any more (contract 1.7)", 'count_beam' not in body)
# t_c/rh (DHT22) are gone (contract 1.8): they only ever fed an adaptive
# cure model that was dropped in 1.2, so the sensor had nothing left to do.
for field in ("t_c", "rh"):
    check('box_done does NOT include "%s" (contract 1.8)' % field,
          ('d["%s"]' % field) not in body)

# The `final` marker (contract 1.7) is what closes the premature-publish
# race: without it, sketch.ino can only guess a box is done from a
# stability TIMEOUT, and ordinary MQTT jitter can clip the last core.
check("onRaw() reads the `final` field off the raw frame",
     'd["final"]' in sketch)
check("a shorter STABLE_MS_FINAL exists for once `final` has arrived",
     "STABLE_MS_FINAL" in sketch)

# The fallback timeout must be longer than the plant model's settle window
# (backend/plant.py::build_arrival pushes 14 settle frames at RAW_HZ), or it
# fires before `final` ever arrives (contract 1.10 finding).
settle_ms = 14 * 1000.0 / C.RAW_HZ
stable_ms = find(r"STABLE_MS\s*=\s*(\d+)\s*;", sketch, "STABLE_MS")
if stable_ms:
    check("STABLE_MS (%s) is longer than the %.0f ms settle window" % (stable_ms, settle_ms),
         int(stable_ms) > settle_ms + 500)

# One box -> one box_done (contract 1.10): publishBoxDone latches instead of
# resetting the tare mid-arrival, and onRaw ignores frames once latched.
check("publishBoxDone() latches g_done instead of calling resetBox()",
     "g_done = true" in body and "resetBox()" not in body)
check("onRaw() returns early once the box is reported",
     re.search(r"void onRaw\([^)]*\)\s*\{\s*if \(g_done\) return;", sketch) is not None)
check("the activity LED is switched off again", "digitalWrite(PIN_LED, LOW)" in sketch)

# resetBox() must restart the stability timer at start_box, not zero it:
# with 0, a crate frame that arrived before start_box tared instantly and
# the next one declared DONE on the empty crate (found before the 1.12 push).
reset_body = re.search(r"void resetBox\(\)\s*\{(.*?)\n\}", sketch, re.S)
check("resetBox() restarts the stability timer (g_stableMs = millis())",
     bool(reset_body) and "g_stableMs = millis()" in reset_body.group(1)
     and "g_stableMs = 0" not in reset_body.group(1))

fake = open(os.path.join(ROOT, "tools", "fake_device.py"), encoding="utf-8").read()
check("tools/fake_device.py reset() restarts its timer the same way",
     "self.stable_since = time.monotonic()" in fake and "self.stable_since = 0" not in fake)
fake_stable = re.search(r"STABLE_S\s*=\s*([\d.]+)", fake)
check("tools/fake_device.py STABLE_S matches sketch.ino STABLE_MS",
     bool(fake_stable and stable_ms) and abs(float(fake_stable.group(1)) * 1000 - int(stable_ms)) < 1)

# --- 4. wiring matches the judging requirement (real, legible pins) --------
required_pins = {"PIN_DONE": "26", "PIN_POT": "34", "PIN_LED": "2"}
for name, pin in required_pins.items():
    got = find(r"#define\s+%s\s+(\d+)" % name, sketch, name)
    if got:
        check("%s is GPIO %s" % (name, pin), got == pin)
check("PIN_DHT is gone from sketch.ino (contract 1.8)", "PIN_DHT" not in sketch)

conns = diagram["connections"]


def wired(a_suffix, b_suffix):
    for c in conns:
        a, b = c[0], c[1]
        if (a.endswith(a_suffix) and b.endswith(b_suffix)) or \
           (a.endswith(b_suffix) and b.endswith(a_suffix)):
            return True
    return False


check("diagram.json: no leftover beam button wiring (contract 1.7)",
     not wired("btnBeam:2.l", "esp:D25"))
check("diagram.json: done button on esp:D26", wired("btnDone:2.l", "esp:D26"))
check("diagram.json: potentiometer signal on esp:D34", wired("pot:SIG", "esp:D34"))
check("diagram.json: no leftover DHT22 part (contract 1.8)",
     not any(p.get("type") == "wokwi-dht22" for p in diagram["parts"]))
check("diagram.json: LED driven from esp:D2 through the resistor",
      wired("esp:D2", "r1:1"))
r1 = next((p for p in diagram["parts"] if p["id"] == "r1"), None)
check("diagram.json: LED series resistor is 220 ohm",
      bool(r1) and r1["attrs"].get("value") == "220")

# --- 5. OLED status display (criterion 9 polish) ---------------------------
oled_part = next((p for p in diagram["parts"] if p["id"] == "oled"), None)
check("diagram.json: an SSD1306 OLED part exists",
     bool(oled_part) and oled_part.get("type") == "wokwi-ssd1306")
check("diagram.json: OLED I2C address is 0x3c",
     bool(oled_part) and oled_part["attrs"].get("i2cAddress") == "0x3c")
check("diagram.json: OLED SDA on esp:D21 (default ESP32 I2C)",
     wired("oled:SDA", "esp:D21"))
check("diagram.json: OLED SCL on esp:D22 (default ESP32 I2C)",
     wired("oled:SCL", "esp:D22"))
check("sketch.ino includes the SSD1306 driver", "Adafruit_SSD1306.h" in sketch)
check("sketch.ino initialises the OLED without blocking on failure",
     "oled.begin(" in sketch and "g_oledOk" in sketch)

libs = open(os.path.join(ROOT, "firmware", "libraries.txt"), encoding="utf-8").read()
for lib in ("Adafruit GFX Library", "Adafruit SSD1306"):
    check('libraries.txt lists "%s"' % lib, lib in libs)
check('libraries.txt does NOT list "DHT sensor library" (contract 1.8)',
      "DHT sensor library" not in libs)

print("\n" + ("ALL GREEN" if not fails else "%d FAILURE(S): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
