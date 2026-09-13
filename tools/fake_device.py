"""
tools/fake_device.py — a Python stand-in for the ESP32.

    python tools/fake_device.py

It subscribes to the SAME raw topic, runs the SAME measurement chain (tare,
debounce, stability, edge counting) and publishes BYTE-IDENTICAL telemetry and
box_done messages. The backend cannot tell it apart from the real board.

Two uses:
  1. P3 develops the backend and the dashboard without waiting for P4's Wokwi.
  2. It is fallback level L1 at the demo: if Wokwi drops, start this in a
     spare terminal and the screen behaves exactly the same.

Never demo this *as* the embedded system. It exists so the embedded system is
never on the critical path of anything else.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config as C

import paho.mqtt.client as mqtt

DEBOUNCE_S = 0.04
STABLE_S = 1.2
STABLE_BAND_G = 25.0


class FakeEsp32:
    def __init__(self) -> None:
        self.reset()
        self.t_c, self.rh = 24.0, 52.0
        self.ref = "NY-114"

    def reset(self) -> None:
        self.count = 0
        self.tare_g = 0.0
        self.gross_g = 0.0
        self.tared = False
        self.stable = False
        self.last_beam = 1
        self.last_edge = 0.0
        self.stable_since = time.monotonic()
        self.last_mass = 0.0
        self.done_sent = False

    # --- identical logic to firmware/sketch.ino ---------------------------
    def on_raw(self, beam: int, load_mv: float, client) -> None:
        now = time.monotonic()
        mass = load_mv * C.G_PER_MV

        if not self.tared:
            if abs(mass - self.last_mass) < STABLE_BAND_G:
                if now - self.stable_since > 0.4:
                    self.tare_g, self.tared = mass, True
                    print("[tare] %.0f g" % self.tare_g)
            else:
                self.stable_since = now
            self.last_mass = mass
            return

        self.gross_g = mass

        if self.last_beam == 1 and beam == 0 and now - self.last_edge > DEBOUNCE_S:
            self.count += 1
            self.last_edge = now
        self.last_beam = beam

        # Weight is the only sensor now (contract 1.5 -- identification moved
        # to a barcode scan upstream of this stand-in); no beam count gate.
        if abs(mass - self.last_mass) > STABLE_BAND_G:
            self.stable_since = now
            self.stable = False
        elif now - self.stable_since > STABLE_S:
            if not self.stable:
                self.stable = True
                self.publish_done(client)
        self.last_mass = mass

    def publish_done(self, client) -> None:
        if self.done_sent:
            return
        self.done_sent = True
        payload = {"ref": self.ref, "count_beam": self.count,
                   "gross_g": round(self.gross_g, 1),
                   "t_c": self.t_c, "rh": self.rh, "fw": "fake-1.0"}
        client.publish(C.T_BOX_DONE, json.dumps(payload))
        print("[box_done] %s" % payload)
        self.reset()

    def telemetry(self) -> str:
        return json.dumps({
            "state": ("STABILIZING" if self.stable else
                      "COUNTING" if self.tared else "IDLE"),
            "count_beam": self.count, "gross_g": round(self.gross_g, 1),
            "stable": self.stable, "t_c": self.t_c, "rh": self.rh,
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
        if d.get("t_c") is not None:
            dev.t_c, dev.rh = d["t_c"], d["rh"]
        dev.on_raw(int(d.get("beam", 1)), float(d.get("load_mv", 0)), client)
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
