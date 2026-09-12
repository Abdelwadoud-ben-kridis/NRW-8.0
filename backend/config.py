"""Everything you might need to change at the venue is in this one file."""
import os

# --- MQTT -------------------------------------------------------------------
# CHANGE THIS in the first 10 minutes at INSAT so you don't collide with
# another team on the same public broker. It must match SESSION in
# firmware/sketch.ino EXACTLY.
SESSION = os.environ.get("SCW_SESSION", "nrw8")

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

# --- storage ----------------------------------------------------------------
DB_PATH = os.environ.get("SCW_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scw.db"))
