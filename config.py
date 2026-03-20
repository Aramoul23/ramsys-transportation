"""
Ramsys School Bus Routing System — Central Configuration
=========================================================
ALL modules import constants from this file.
NEVER hardcode school coordinates, costs, time windows, or tuning
factors anywhere else.

HOW TO VERIFY SCHOOL COORDINATES:
  1. Open Google Maps on your phone or PC
  2. Search "Ramsys School UV 10 Ali Mendjeli Constantine"
  3. Long-press on the school building to drop a pin
  4. Read the lat, lon shown at the bottom of the screen
  5. Update SCHOOL_LAT and SCHOOL_LON below, then restart Flask
"""

import sqlite3

# ============================================================
# SCHOOL — SINGLE SOURCE OF TRUTH
# ============================================================
SCHOOL_NAME    = "Ramsys School"
SCHOOL_ADDRESS = "UV 10, Nouvelle Ville Ali Mendjeli, Constantine, Algeria"
SCHOOL_LAT     = 36.24502366420027
SCHOOL_LON     = 6.579864240305483

# ============================================================
# TIME WINDOWS (24-hour format strings)
# ============================================================
MORNING_DEPARTURE_TIME   = "07:15:00"
MORNING_DEADLINE_TIME    = "08:30:00"
AFTERNOON_DEPARTURE_TIME = "16:00:00"
AFTERNOON_DEADLINE_TIME  = "17:30:00"

# Numeric versions used for ETA calculation and hard route-time cap
MORNING_DEPARTURE_HOUR   = 7
MORNING_DEPARTURE_MINUTE = 15
MORNING_DEADLINE_HOUR    = 8    # used in optimize_routes.py to enforce 08:30 cutoff
MORNING_DEADLINE_MINUTE  = 30

AFTERNOON_DEPARTURE_HOUR   = 16
AFTERNOON_DEPARTURE_MINUTE = 0
AFTERNOON_DEADLINE_HOUR    = 17
AFTERNOON_DEADLINE_MINUTE  = 30

# ============================================================
# FLEET ECONOMICS
# ============================================================
DAILY_COST_PER_BUS_DZD = 5000

# ============================================================
# DATABASE
# ============================================================
DATABASE = "ramsys_routing.db"

# ============================================================
# AUTHENTICATION
# ============================================================
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "ramsys2026"   # Change this in production!

# ============================================================
# GEOGRAPHY & CALENDAR
# ============================================================
TIMEZONE_OFFSET = "+01:00"
WEEKEND_DAYS = [4, 5]   # 4=Friday, 5=Saturday

# ============================================================
# OPTIMIZER DEFAULTS
# ============================================================
DEFAULT_CYCLE_SEPARATION        = True
DEFAULT_ALLOW_SIBLING_MIXING    = True
DEFAULT_MAX_STOPS_PER_BUS       = 20
# 07:15 -> 08:30 = 75 min actual window (was wrongly 90 before)
DEFAULT_MAX_ROUTE_MINUTES       = 75
DEFAULT_SOLVER_TIME_LIMIT_SECS  = 120
DEFAULT_MIN_BUS_UTILIZATION_PCT = 60

# ============================================================
# OPTIMIZER TUNING
# ============================================================
# Straight-line (haversine) to real road distance multiplier.
# Ali Mendjeli has a dense urban grid; 1.3 is a good baseline.
# Increase if buses consistently arrive late; decrease if too early.
ROAD_FACTOR = 1.3

# Maximum penalty (%) when considering farthest-first reorder.
# If reorder would add more than this, keep OR-Tools sequence.
REORDER_MAX_PENALTY_PCT = 10

# ============================================================
# NEW STUDENT INSERTION
# ============================================================
MAX_INSERTION_DISTANCE_METERS = 500
SIBLING_DISTANCE_METERS       = 50

# ============================================================
# SIBLING AUDIT
# ============================================================
AUDIT_SUSPICIOUS_STUDENT_COUNT = 4   # Flag families with >= N students
AUDIT_SUSPICIOUS_PHONE_COUNT   = 2   # Flag families with >= N distinct phones


# ============================================================
# DATABASE HELPER — shared by all modules
# ============================================================
def get_db_connection():
    """
    Create a properly configured SQLite connection.
    - WAL mode for better concurrency
    - 30s busy timeout to handle concurrent requests
    - Foreign keys enforced
    - Row factory for dict-like access
    """
    conn = sqlite3.connect(DATABASE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
