"""
tools/fake_device.py — a Python stand-in for the ESP32.

    python tools/fake_device.py

It subscribes to the SAME raw topic, runs the SAME measurement chain as
firmware/sketch.ino (tare, stability, `final`-frame shortcut, one box_done
per box then latch until the next start_box) and publishes the same
telemetry and box_done payload shapes. The backend cannot tell it apart
from the real board.

Two uses:
  1. P3 develops the backend and the dashboard without waiting for P4's Wokwi.
  2. It is fallback level L1b at the demo: if Wokwi drops, start this in a
     spare terminal and the screen behaves exactly the same.

Never demo this *as* the embedded system. It exists so the embedded system is
never on the critical path of anything else.

Keep on_raw() a line-for-line mirror of sketch.ino::onRaw(): contract 1.10
found the two had drifted (this file reset its stability timer to "now",
the sketch to 0), which hid the sketch publishing box_done twice per box
from tools/l0_probe.py.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config as C

import paho.mqtt.client as mqtt

STABLE_S = 2.5           # fallback only -- sketch.ino STABLE_MS
STABLE_S_FINAL = 0.25    # once the conveyor said "final" -- sketch.ino STABLE_MS_FINAL
STABLE_BAND_G = 25.0


class FakeEsp32:
    def __init__(self) -> None:
        self.last_mass = 0.0      # like g_lastMass: NOT cleared by reset
        self.ref = "NY-114"
        self.reset()

    def reset(self) -> None:      # sketch.ino::resetBox()
        self.tare_g = 0.0
        self.gross_g = 0.0
        self.tared = False
        self.counting = False
        self.stable = False
        self.saw_final = False
        # like g_stableMs = millis(): the timers start at start_box. 0 made
        # "now - stable_since" huge, so a crate frame that beat start_box
        # across topics tared at once and declared DONE on the crate alone.
        self.stable_since = time.monotonic()
        self.done = False

    # --- identical logic to firmware/sketch.ino::onRaw() ------------------
    def on_raw(self, load_mv: float, final: bool, client) -> None:
        if self.done:
            return
        now = time.monotonic()
        mass = load_mv * C.G_PER_MV

        if not self.tared:
            if abs(mass - self.last_mass) < STABLE_BAND_G:
                if now - self.stable_since > 0.4:
                    self.tare_g, self.tared, self.counting = mass, True, True
                    print("[tare] %.0f g" % self.tare_g)
            else:
                self.stable_since = now
            self.last_mass = mass
            return

        self.gross_g = mass
        if final:
            self.saw_final = True

        need = STABLE_S_FINAL if self.saw_final else STABLE_S
        if abs(mass - self.last_mass) > STABLE_BAND_G:
            self.stable_since = now
            self.stable = False
        elif now - self.stable_since > need:
            if not self.stable:
                self.stable = True
                self.publish_done(client)
        self.last_mass = mass

    def publish_done(self, client) -> None:   # sketch.ino::publishBoxDone()
        payload = {"ref": self.ref, "gross_g": round(self.gross_g, 1),
                   "fw": "fake-1.3"}
        client.publish(C.T_BOX_DONE, json.dumps(payload))
        print("[box_done] %s" % payload)
        self.done, self.counting, self.stable = True, False, True

    def telemetry(self) -> str:
        state = ("DONE" if self.done else
                 ("STABILIZING" if self.stable else "COUNTING") if self.counting
                 else "IDLE")
        return json.dumps({
            "state": state,
            "gross_g": round(self.gross_g, 1),
            "stable": self.stable,
            "up_ms": int(time.monotonic() * 1000), "src": "fake"})


dev = FakeEsp32()


def on_connect(client, *_a):
    client.subscribe(C.T_RAW)
    client.subscribe(C.T_CMD)
    print("[fake] connected, listening on %s" % C.T_RAW)


def on_message(client, _u, msg):
    try:
        d = json.loads(msg.payload.decode())
    except Exception:
        return
    if msg.topic == C.T_RAW:
        dev.on_raw(float(d.get("load_mv", 0)), bool(d.get("final", False)), client)
    elif msg.topic == C.T_CMD:
        cmd = d.get("cmd")
        if cmd == "start_box":
            dev.reset()
            dev.ref = d.get("ref", "NY-114")
            print("[cmd] start_box %s" % dev.ref)
        elif cmd == "reset":
            dev.reset()
        elif cmd == "tare":
            dev.tared = False


def main() -> None:
    try:                                        # paho 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id="scw-fake-%d" % (time.time() % 100000))
    except Exception:                           # paho 1.x
        client = mqtt.Client(client_id="scw-fake-%d" % (time.time() % 100000))
    client.on_connect = on_connect
    client.on_message = on_message
    print("[fake] %s:%d  session=%s" % (C.MQTT_HOST, C.MQTT_PORT, C.SESSION))
    client.connect(C.MQTT_HOST, C.MQTT_PORT, 30)
    client.loop_start()
    try:
        while True:
            client.publish(C.T_TELEMETRY, dev.telemetry())
            time.sleep(0.5)
    except KeyboardInterrupt:
        client.loop_stop()


if __name__ == "__main__":
    main()
