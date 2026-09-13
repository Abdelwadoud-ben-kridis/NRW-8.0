"""
tools/record_replay.py — records a real demo run for the GitHub Pages replay.

    SCW_BASE=http://127.0.0.1:8793 SCW_SESSION=<that server's session> \
        python tools/record_replay.py                  -> pages/replay.json

This calls /api/reset, loads the demo scenario and plays the beats of
docs/demo-script.md, so it REFUSES a server on port 8000 (your working
database): start an isolated instance with its own SCW_DB and SCW_SESSION.
It launches tools/fake_device.py (same session) so the recording shows the
live L0 device path, not the L1 fallback.

What is stored: every /ws state snapshot as top-level-key patches (keys whose
only change is a float -- cure timers, clock -- are throttled to 1 Hz), the
GET answers the dashboard polls, and the Database Explorer's views at the end
of the run. tools/build_pages.py bundles it with the dashboard, and
dashboard/replay.js plays it through the same render() the WebSocket feeds.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("SCW_BASE", "http://localhost:8000").rstrip("/")
OUT = os.path.join(ROOT, "pages", "replay.json")

# re-sent on any change: these drive the crane animation, the pills and panels
FAST = {"crane", "device", "clock_label", "t_sim", "speed", "mode", "banner",
        "last_order", "kpi", "mqtt", "orders_pending", "batches_pending"}
POLL = ["/events?limit=60", "/db/check", "/barcodes?unused=true", "/articles", "/anomalies"]
PAGE = 50            # db.js TB.limit

if (urllib.parse.urlparse(BASE).port or 80) == 8000:
    sys.exit("refusing %s: this resets the database. Point SCW_BASE at an isolated "
             "server (its own SCW_DB and SCW_SESSION)." % BASE)


def call(path, body=None):
    req = urllib.request.Request(
        BASE + "/api" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def _shape(v):
    """The value with every float blanked: equal shapes = only timers moved."""
    if isinstance(v, float):
        return None
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_shape(x) for x in v]
    return v


class Recorder:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.frames: list = []
        self.last: dict = {}
        self.sent: dict = {}
        self.latest: dict | None = None
        self.gets = {p: [] for p in POLL}
        self.stop = False

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def add_state(self, st: dict, force: bool = False) -> None:
        t = self.now()
        patch = {}
        for k, v in st.items():
            if k in self.last and self.last[k] == v:
                continue
            if (not force and k in self.last and k not in FAST
                    and _shape(v) == _shape(self.last[k])
                    and t - self.sent.get(k, -9.0) < 1.0):
                continue
            patch[k] = v
            self.last[k] = v
            self.sent[k] = t
        if patch:
            self.frames.append([t, patch])


async def ws_task(rec: Recorder) -> None:
    async with websockets.connect(BASE.replace("http", "ws", 1) + "/ws",
                                  max_size=50_000_000) as ws:
        while not rec.stop:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "state":
                rec.latest = msg
                rec.add_state(msg)


async def poll_task(rec: Recorder) -> None:
    while not rec.stop:
        for p in POLL:
            v = await asyncio.to_thread(call, p)
            series = rec.gets[p]
            if not series or series[-1][1] != v:
                series.append([rec.now(), v])
        await asyncio.sleep(1.0)


def box_of(box_id: str) -> dict:
    return next(b for b in call("/state")["boxes"] if b["box_id"] == box_id)


async def beats() -> None:
    c = lambda p, b=None: asyncio.to_thread(call, p, b)      # noqa: E731
    say = lambda *a: print("  [%s]" % time.strftime("%H:%M:%S"), *a)  # noqa: E731
    hold = asyncio.sleep

    await c("/scenario", {"name": "demo"}); say("S  demo scenario")
    await hold(6)
    await c("/barcodes", {"barcode_id": "BC-1042", "ref": "NY-114", "unit_mass_g": 206.0})
    say("barcode BC-1042 registered")
    await hold(2)
    r = await c("/sim/arrival", {"barcode_id": "BC-1042", "qty": 37, "anomaly": "none"})
    say("A  clean arrival", r)
    await hold(9)
    r = await c("/sim/arrival", {"ref": "NY-114", "qty": 37, "anomaly": "mismatch"})
    mismatch = r.get("box_id"); say("A  mismatch arrival", r)
    await hold(7)

    # operator's choice: a newer ready box is allowed, the FIFO skip is recorded
    o = await c("/demand/box", {"box_id": "BOX-3"})
    say("D  BOX-3", o.get("status"), "fifo_override=%s" % o.get("fifo_override"))
    await hold(7)
    await c("/demand/cancel", {"order_id": o["order_id"]}); say("cancel", o["order_id"])
    await hold(3)
    o = await c("/demand/oldest", {})
    say("D  FIFO head", o.get("box_requested"), o.get("status"))
    await hold(5)
    await c("/demand/confirm", {"order_id": o["order_id"]}); say("C  confirm", o["order_id"])
    await hold(22)                                   # the crane's pick cycle

    await c("/clock", {"jump_h": 6}); say("J  +6 h")
    await hold(7)
    if mismatch:
        r = await c("/box/%s/recount" % mismatch, {"gross_g": box_of(mismatch)["gross_g"]})
        say("re-weigh %s" % mismatch, r.get("accepted"), r.get("reason"))
        await hold(6)
        await c("/box/%s/archive" % mismatch, {}); say("archive", mismatch)
        await hold(7)


def capture_db() -> dict:
    db = {p: call("/db" + p) for p in ("/schema", "/tables", "/stats", "/check")}
    box_ids = []
    for t in db["/schema"].get("tables", []):
        name, total = t["name"], int(t.get("row_count") or 0)
        for off in range(0, min(max(total, 1), 20 * PAGE), PAGE):
            page = call("/db/table/%s?limit=%d&offset=%d&q=" % (name, PAGE, off))
            db["/table/%s?limit=%d&offset=%d&q=" % (name, PAGE, off)] = page
            if name == "boxes":
                box_ids += [row["box_id"] for row in page.get("rows", [])]
    for bid in box_ids:
        db["/box/%s" % urllib.parse.quote(bid, safe="")] = call("/db/box/%s" % bid)
    return db


async def main() -> None:
    print("recording against", BASE)
    dev = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fake_device.py")],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not call("/state")["device"].get("online"):     # same warm-up as l0_probe
            if time.monotonic() > deadline:
                sys.exit("fake_device never came online on the broker")
            await asyncio.sleep(0.5)
        for _ in range(4):
            await asyncio.sleep(1.0)
            call("/reset", {})
            if call("/sim/arrival", {"ref": "NY-114", "qty": 30}).get("mode") == "L0":
                break
        else:
            sys.exit("fake_device never answered a warm-up arrival in L0")
        call("/reset", {})
        await asyncio.sleep(1.0)

        rec = Recorder()
        tasks = [asyncio.create_task(ws_task(rec)), asyncio.create_task(poll_task(rec))]
        await asyncio.sleep(2.0)
        await beats()
        rec.stop = True
        await asyncio.gather(*tasks)
        if rec.latest:
            rec.add_state(rec.latest, force=True)
        duration = rec.now()
        db = capture_db()
    finally:
        dev.terminate()

    if not rec.frames:
        sys.exit("no WebSocket snapshots received")
    first = rec.frames[0][1]
    out = {
        "meta": {"recorded_utc": datetime.datetime.now(datetime.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "contract_version": first.get("contract_version"),
                 "duration_s": duration},
        "frames": rec.frames,
        "gets": rec.gets,
        "db": db,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    modes = sorted({p["mode"] for _, p in rec.frames if "mode" in p})
    print("wrote %s: %.0f s, %d frames, modes %s, %.0f KB"
          % (os.path.relpath(OUT, ROOT), duration, len(rec.frames), modes,
             os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    asyncio.run(main())
