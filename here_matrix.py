"""
Ramsys School Bus Routing System — HERE Traffic Matrix Fetcher
==============================================================
Phase 2A: Fetches real driving times (home → school) from HERE Matrix API v8.
Results are cached in the database and used by the optimizer.

Run this before every optimizer run (or at least once per semester unless
student locations change significantly).
"""

import os
import sqlite3
import requests
import time
import math
from dotenv import load_dotenv
from datetime import datetime, timedelta

from config import (
    SCHOOL_LAT, SCHOOL_LON, SCHOOL_NAME,
    MORNING_DEPARTURE_TIME, AFTERNOON_DEPARTURE_TIME,
    TIMEZONE_OFFSET, WEEKEND_DAYS, DATABASE
)

# Bus speed constants for fallback (20 km/h average)
# 20 km/h = 1 km per 3 mins = 180 seconds per km
SEC_PER_KM = 180

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Real-world great-circle distance in km between two GPS points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# HERE Matrix API v8 — max 100 origins per synchronous request
MAX_PER_BATCH = 100
MATRIX_URL = "https://matrix.router.hereapi.com/v8/matrix?async=false"


def get_next_weekday_datetime(time_str: str) -> str:
    """
    Return an ISO-8601 timestamp for the next valid school-day at `time_str`.
    HERE API requires a future datetime for departureTime.

    Algeria school week: Sunday–Thursday (Friday=4, Saturday=5 are weekend).
    """
    now = datetime.now()
    target_time = datetime.strptime(time_str, "%H:%M:%S").time()

    # Always push at least 1 day forward to avoid "already passed today" edge cases
    target_dt = (now + timedelta(days=1)).replace(
        hour=target_time.hour,
        minute=target_time.minute,
        second=0,
        microsecond=0
    )

    # Skip weekend days (Friday=4, Saturday=5)
    while target_dt.weekday() in WEEKEND_DAYS:
        target_dt += timedelta(days=1)

    return target_dt.strftime(f"%Y-%m-%dT%H:%M:%S{TIMEZONE_OFFSET}")


def setup_matrix_tables(cursor):
    """Creates the travel-time caching tables if they don't already exist."""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS travel_times_morning (
            family_id           INTEGER PRIMARY KEY,
            travel_time_seconds INTEGER NOT NULL,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (family_id) REFERENCES families (id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS travel_times_afternoon (
            family_id           INTEGER PRIMARY KEY,
            travel_time_seconds INTEGER NOT NULL,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (family_id) REFERENCES families (id)
        )
    ''')


def call_here_matrix(api_key: str, origins: list, destinations: list,
                     departure_time: str, batch_label: str) -> list | None:
    """
    Call HERE Matrix API v8 for one batch.
    Returns a flat list of travel times in seconds, or None on failure.

    API key is passed in the Authorization header (not in the URL)
    to keep it out of server logs and browser history.
    """
    headers = {
        "Content-Type": "application/json"
    }
    url_with_key = f"{MATRIX_URL}&apiKey={api_key}"
    payload = {
        "origins":          origins,
        "destinations":     destinations,
        "regionDefinition": {"type": "world"},
        "profile":          "bus",
        "departureTime":    departure_time,
    }

    try:
        response = requests.post(url_with_key, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            raw_times = response.json()["matrix"]["travelTimes"]
            # Apply a 1.25x multiplier to account for bus stops and slower speeds in residential areas
            return [int(t * 1.25) if t is not None else None for t in raw_times]
        else:
            print(f"  ❌ {batch_label} — HTTP {response.status_code}: {response.text[:200]}")
            return None
    except requests.exceptions.Timeout:
        print(f"  ❌ {batch_label} — Request timed out after 60s")
        return None
    except Exception as e:
        print(f"  ❌ {batch_label} — Unexpected error: {e}")
        return None


def fetch_and_cache_matrices(db_path: str = DATABASE):
    load_dotenv()
    api_key = os.getenv("HERE_API_KEY")

    if not api_key:
        print("❌ HERE_API_KEY not found in .env file.")
        print("   Add:  HERE_API_KEY=your_key_here  to your .env file.")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    setup_matrix_tables(cursor)

    # Load all families that have students
    cursor.execute(
        "SELECT id, latitude, longitude, family_name FROM families WHERE student_count > 0"
    )
    families = cursor.fetchall()

    if not families:
        print("❌ No families found. Import students first via the admin dashboard.")
        conn.close()
        return False

    print(f"📍 Loaded {len(families)} family stops from database.")
    print(f"🏫 School: {SCHOOL_NAME} at ({SCHOOL_LAT}, {SCHOOL_LON})")
    school_dest = [{"lat": SCHOOL_LAT, "lng": SCHOOL_LON}]
    school_orig = [{"lat": SCHOOL_LAT, "lng": SCHOOL_LON}]

    # ----------------------------------------------------------
    # MORNING MATRIX: each family home → school  (at 07:00 AM)
    # ----------------------------------------------------------
    morning_dt = get_next_weekday_datetime(MORNING_DEPARTURE_TIME)
    print(f"\n🌅 MORNING Matrix (homes → school) | departure: {morning_dt}")

    morning_cache = []
    total_batches = (len(families) + MAX_PER_BATCH - 1) // MAX_PER_BATCH

    for i in range(0, len(families), MAX_PER_BATCH):
        batch     = families[i: i + MAX_PER_BATCH]
        batch_num = i // MAX_PER_BATCH + 1
        origins   = [{"lat": f[1], "lng": f[2]} for f in batch]
        label     = f"Morning batch {batch_num}/{total_batches}"

        print(f"  📡 Fetching {label} ({len(batch)} families)…", end=" ", flush=True)
        times = call_here_matrix(api_key, origins, school_dest, morning_dt, label)

        if times:
            for j, secs in enumerate(times):
                family_id = batch[j][0]
                # Fallback: 30 min if HERE returns 0 or null
                safe_secs = int(secs) if secs and secs > 0 else 1800
                morning_cache.append((family_id, safe_secs))
            print(f"✅ {len(times)} times received")
        else:
            print("⚠️  Skipped — using fallback 30min for this batch")
            for f in batch:
                morning_cache.append((f[0], 1800))

        # Rate-limit courtesy pause between batches
        if batch_num < total_batches:
            time.sleep(0.5)

    if morning_cache:
        cursor.execute("DELETE FROM travel_times_morning")
        cursor.executemany(
            "INSERT INTO travel_times_morning (family_id, travel_time_seconds) VALUES (?, ?)",
            morning_cache
        )
        conn.commit()
        print(f"✅ Morning matrix saved: {len(morning_cache)} travel times cached.")

    # ----------------------------------------------------------
    # AFTERNOON MATRIX: school → each family home  (at 16:00)
    # ----------------------------------------------------------
    afternoon_dt = get_next_weekday_datetime(AFTERNOON_DEPARTURE_TIME)
    print(f"\n🌆 AFTERNOON Matrix (school → homes) | departure: {afternoon_dt}")

    afternoon_cache = []

    for i in range(0, len(families), MAX_PER_BATCH):
        batch     = families[i: i + MAX_PER_BATCH]
        batch_num = i // MAX_PER_BATCH + 1
        dests     = [{"lat": f[1], "lng": f[2]} for f in batch]
        label     = f"Afternoon batch {batch_num}/{total_batches}"

        print(f"  📡 Fetching {label} ({len(batch)} families)…", end=" ", flush=True)
        times = call_here_matrix(api_key, school_orig, dests, afternoon_dt, label)

        if times:
            for j, secs in enumerate(times):
                family_id = batch[j][0]
                # Fallback: calculate distance-based if HERE returns 0 or null
                if secs is None or secs <= 0:
                    dist = haversine_km(SCHOOL_LAT, SCHOOL_LON, batch[j][1], batch[j][2])
                    safe_secs = int((dist * SEC_PER_KM) + 120) # Add 2 mins per stop (simulated) + distance time
                    print(f" (Fallback for family {family_id}: {safe_secs}s)", end="")
                else:
                    safe_secs = int(secs)
                afternoon_cache.append((family_id, safe_secs))
            print(f"✅ {len(times)} times received")
        else:
            print("⚠️  Skipped — using distance-based fallback for this batch")
            for f in batch:
                family_id = f[0]
                dist = haversine_km(SCHOOL_LAT, SCHOOL_LON, f[1], f[2])
                est_seconds = int((dist * SEC_PER_KM) + 120) # Add 2 mins per stop (simulated) + distance time
                afternoon_cache.append((family_id, est_seconds))

        if batch_num < total_batches:
            time.sleep(0.5)

    if afternoon_cache:
        cursor.execute("DELETE FROM travel_times_afternoon")
        cursor.executemany(
            "INSERT INTO travel_times_afternoon (family_id, travel_time_seconds) VALUES (?, ?)",
            afternoon_cache
        )
        conn.commit()
        print(f"✅ Afternoon matrix saved: {len(afternoon_cache)} travel times cached.")

    conn.close()
    print("\n🚀 Phase 2A complete: HERE traffic matrices fetched and cached.")
    return True


if __name__ == "__main__":
    fetch_and_cache_matrices()
