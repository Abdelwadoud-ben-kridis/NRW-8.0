"""Everything you might need to change at the venue is in this one file."""
import os

# --- MQTT -------------------------------------------------------------------
# CHANGE THIS in the first 10 minutes at INSAT so you don't collide with
# another team on the same public broker. It must match SESSION in
# firmware/sketch.ino EXACTLY.
SESSION = os.environ.get("SCW_SESSION", "nrw8-scw-k7q2")

MQTT_HOST = os.environ.get("SCW_MQTT_HOST", "broker.hivemq.com")
MQTT_PORT = int(os.environ.get("SCW_MQTT_PORT", "1883"))

T_RAW = f"scw/{SESSION}/sim/raw"
T_TELEMETRY = f"scw/{SESSION}/dev/telemetry"
T_BOX_DONE = f"scw/{SESSION}/dev/box_done"
T_CMD = f"scw/{SESSION}/dev/cmd"

# --- warehouse geometry (curing room is 6 x 6 x 6 m) ------------------------
FACES = 2
COLS = 9
LEVELS = 17                     # 2 x 9 x 17 = 306 slots
                                 # sized for a 51.5x32.5x17.5 cm crate + 10 cm
                                 # clearance per slot on both axes
SLOT_COUNT = FACES * COLS * LEVELS

RACK_X_M = 5.6                  # 93 % of the 6 m room length
RACK_Z_M = 4.8                  # 80 % of the 6 m ceiling, 1.2 m clearance
CRANE_VX = 1.2                  # m/s travel
CRANE_VZ = 0.8                  # m/s lift
FORK_REACH_MM = 450

# --- physics shared with the firmware ---------------------------------------
TARE_G = 1800.0
MV_FULL_SCALE = 3300.0
G_FULL_SCALE = 30000.0
G_PER_MV = G_FULL_SCALE / MV_FULL_SCALE     # 9.0909

# --- clock ------------------------------------------------------------------
CLOCK_START_SIM = 0.0
DEFAULT_SPEED = 60.0            # 1 real second = 1 simulated minute
WS_HZ = 5.0
RAW_HZ = 10.0
MAX_JUMP_H = 48.0                # /api/clock jump_h is clamped to (0, MAX_JUMP_H]
ALLOWED_SPEEDS = (0.0, 1.0, 60.0, 3600.0)
# The rehearsed demo scenario (S hotkey) starts the clock at x1, not x60
# (contract 1.10): at x60 the scripted numbers drifted within minutes (BOX-4
# cured 1 real minute after S, BOX-5 after 9), and a reservation's 2 sim-h
# lock expired after 2 real minutes. The clock still visibly ticks; use J
# (+6 h) to cross the 24 h threshold on demand.
SCENARIO_SPEED = 1.0

# --- storage ----------------------------------------------------------------
DB_PATH = os.environ.get("SCW_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scw.db"))

# --- warehouse policy (contract 1.2) -----------------------------------------
# The CDC (cahier des charges) fixes ONE drying requirement -- 24 h, the same
# for every box, every reference, every climate. There is no climate sensor
# or input anywhere any more (contract 1.8 removed the DHT22). Do not
# resurrect an adaptive model here.
CURE_H = 24.0

# A RESERVED box auto-releases after this many SIMULATED hours if nobody
# confirms or cancels the order. Chosen so the ~1:30 of talking between
# pressing D and pressing C at demo speed x60 (90 real s = 1.5 sim h) does not
# race the lock: 2.0 sim-h = 2 real minutes at x60, 2 real hours at x1.
LOCK_TTL_H = 2.0

# --- idempotency / arrival dedup (runtime-only, no new MQTT fields) ---------
ARRIVAL_GRACE_S = 15.0           # real seconds a late box_done can still be
                                  # recognised as resolving the open arrival
                                  # window, after L1 already fired
UNSOLICITED_DEDUP_S = 5.0        # real seconds within which an identical
                                  # unsolicited box_done (no open window,
                                  # same barcode+gross_g+fw) is a
                                  # repeat, not a second physical box
DEVICE_LIVENESS_S = 5.0          # real (monotonic) seconds since the last
                                  # telemetry/box_done before the ESP32 pill
                                  # goes offline -- transport-domain, not t_sim

CONTRACT_VERSION = "1.11"
