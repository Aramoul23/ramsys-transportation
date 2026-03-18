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

# ============================================================
# FLEET ECONOMICS
# ============================================================
DAILY_COST_PER_BUS_DZD = 5000

# ============================================================
# DATABASE
# ============================================================
DATABASE = "ramsys_routing.db"

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
DEFAULT_SOLVER_TIME_LIMIT_SECS  = 45
DEFAULT_MIN_BUS_UTILIZATION_PCT = 60

# ============================================================
# OPTIMIZER TUNING
# ============================================================
# Straight-line (haversine) to real road distance multiplier.
# Ali Mendjeli has a dense urban grid; 1.3 is a good baseline.
# Increase if buses consistently arrive late; decrease if too early.
ROAD_FACTOR = 1.3

# ============================================================
# NEW STUDENT INSERTION
# ============================================================
MAX_INSERTION_DISTANCE_METERS = 500
SIBLING_DISTANCE_METERS       = 50
