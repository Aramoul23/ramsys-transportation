"""
Ramsys School Bus Routing System — Database Initializer
========================================================
USAGE:
  Normal first-time setup:   python init_db.py
  Force-reset existing DB:   python init_db.py --force

WARNING: --force permanently deletes ALL data (students, routes, buses).
         Only use this to start completely fresh during initial setup.
"""

import sqlite3
import os
import sys
import shutil
import logging
from datetime import datetime
from config import DATABASE, DEFAULT_CYCLE_SEPARATION, DEFAULT_MAX_STOPS_PER_BUS
from config import DEFAULT_MAX_ROUTE_MINUTES, DEFAULT_SOLVER_TIME_LIMIT_SECS
from config import DEFAULT_MIN_BUS_UTILIZATION_PCT, DEFAULT_ALLOW_SIBLING_MIXING

logger = logging.getLogger(__name__)


def create_database(force=False):
    # --------------------------------------------------------
    # Safety guard — never silently destroy production data
    # --------------------------------------------------------
    if os.path.exists(DATABASE):
        if not force:
            logger.warning(f"Database '{DATABASE}' already exists.")
            logger.warning("Use --force flag to destroy and recreate it.")
            return False

        backup_name = f"{DATABASE}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(DATABASE, backup_name)
        logger.info(f"Backup saved → {backup_name}")

        answer = input(f"⚠️  This will DELETE all data in '{DATABASE}'. Type YES to confirm: ")
        if answer.strip() != "YES":
            logger.info("Aborted. Database untouched.")
            return False

        os.remove(DATABASE)
        logger.info(f"Old database removed.")

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")

    # --------------------------------------------------------
    # 1. FAMILIES — routing nodes (one per home address)
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS families (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        family_name    TEXT    NOT NULL,
        latitude       REAL    NOT NULL,
        longitude      REAL    NOT NULL,
        student_count  INTEGER NOT NULL DEFAULT 1,
        zone           TEXT    DEFAULT "Unknown",
        phone_number   TEXT,
        cycle_profile  TEXT    DEFAULT "MIXED"
    )
    ''')

    # --------------------------------------------------------
    # 2. STUDENTS — linked to families
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS students (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name   TEXT    NOT NULL,
        last_name    TEXT    NOT NULL,
        family_id    INTEGER,
        original_lat REAL,
        original_lon REAL,
        is_active    BOOLEAN DEFAULT 1,
        address      TEXT,
        cycle        TEXT    DEFAULT "Primary",
        FOREIGN KEY (family_id) REFERENCES families (id)
    )
    ''')

    # --------------------------------------------------------
    # 3. BUSES — fleet management
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS buses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_name TEXT    NOT NULL,
        capacity    INTEGER NOT NULL,
        bus_type    TEXT    NOT NULL,
        is_active   BOOLEAN DEFAULT 1
    )
    ''')

    # --------------------------------------------------------
    # 4. ROUTE STOPS — optimizer output, what drivers see
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS route_stops (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_id                INTEGER NOT NULL,
        family_id             INTEGER NOT NULL,
        stop_sequence         INTEGER NOT NULL,
        estimated_pickup_time TEXT    NOT NULL,
        session               TEXT    NOT NULL DEFAULT 'morning',
        departure_time        TEXT,
        FOREIGN KEY (bus_id)    REFERENCES buses    (id),
        FOREIGN KEY (family_id) REFERENCES families (id)
    )
    ''')

    # --------------------------------------------------------
    # 5. TRAVEL TIMES CACHE — from HERE API
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # 6. SCENARIO CONFIG — admin-tunable optimizer parameters
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS scenario_config (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        description TEXT
    )
    ''')

    scenario_defaults = [
        ('cycle_separation',        'true' if DEFAULT_CYCLE_SEPARATION else 'false',
         'Separate Kindergarten/Primary from Middle/High School buses'),
        ('allow_sibling_mixing',    'true' if DEFAULT_ALLOW_SIBLING_MIXING else 'false',
         'Allow siblings (MIXED families) to bridge KP/MH buses'),
        ('max_stops_per_bus',       str(DEFAULT_MAX_STOPS_PER_BUS),
         'Maximum number of pickup stops per bus per route'),
        ('max_route_minutes',       str(DEFAULT_MAX_ROUTE_MINUTES),
         'Maximum total route duration per bus in minutes'),
        ('solver_time_limit_seconds', str(DEFAULT_SOLVER_TIME_LIMIT_SECS),
         'How long OR-Tools is allowed to search for a better solution'),
        ('min_bus_utilization_pct', str(DEFAULT_MIN_BUS_UTILIZATION_PCT),
         'Target minimum fill percentage before deploying an extra bus'),
    ]
    cursor.executemany(
        'INSERT OR IGNORE INTO scenario_config (key, value, description) VALUES (?, ?, ?)',
        scenario_defaults
    )

    # --------------------------------------------------------
    # 7. TRIP EVENTS — driver attendance tracking
    #    family_id has NO FK constraint: metadata events use family_id=0
    # --------------------------------------------------------
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trip_events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_id           INTEGER  NOT NULL,
        family_id        INTEGER  NOT NULL,
        stop_sequence    INTEGER  NOT NULL,
        event_type       TEXT     NOT NULL,
        boarded_count    INTEGER  DEFAULT 0,
        absent_count     INTEGER  DEFAULT 0,
        boarded_names    TEXT,
        absent_names     TEXT,
        actual_time      TEXT     NOT NULL,
        session          TEXT     DEFAULT 'morning',
        notes            TEXT
    )
    ''')

    # --------------------------------------------------------
    # 8. INDEXES — critical for query performance at 300+ students
    # --------------------------------------------------------
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_trip_events_bus_date ON trip_events (bus_id, actual_time)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_route_stops_bus    ON route_stops(bus_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_route_stops_family ON route_stops(family_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_students_family    ON students(family_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_students_is_active ON students(is_active)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_families_name      ON families(family_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_families_active    ON families(student_count)')

    # --------------------------------------------------------
    # 8. SEED FLEET — initial buses (edit to match real fleet)
    # --------------------------------------------------------
    buses_data = [
        ('Driver Ahmed', 30, 'standard', 1),
        ('Driver Karim', 30, 'standard', 1),
        ('Driver Omar', 30, 'standard', 1),
        ('Driver Youssef', 30, 'standard', 1),
        ('Driver Bilal', 30, 'standard', 1),
        ('Driver Samir', 30, 'standard', 1),
        ('Driver Ali', 30, 'standard', 1),
        ('Driver Hassan', 30, 'standard', 1),
        ('Driver Tarik', 30, 'standard', 1),
        ('Driver Mourad', 30, 'standard', 1),
        ('Driver Rachid', 30, 'standard', 1),
        ('Driver Hocine', 30, 'standard', 1),
        ('Driver Sofiane', 30, 'standard', 1),
        ('Driver Abdelkader', 30, 'standard', 1),
        ('Driver Djamel', 30, 'standard', 1),
        ('Driver Khaled', 25, 'coaster', 1),
        ('Driver Larbi', 25, 'coaster', 1),
        ('Driver Moussa', 25, 'coaster', 1),
        ('Driver Nabil Mini', 12, 'mini', 1),
        ('Driver Farid Mini', 12, 'mini', 1),
        ('Driver Yacine Mini', 12, 'mini', 1),
    ]  # Total: 15x30 + 3x25 + 3x12 = 561 seats
    cursor.executemany(
        'INSERT INTO buses (driver_name, capacity, bus_type, is_active) VALUES (?, ?, ?, ?)',
        buses_data
    )

    conn.commit()
    conn.close()
    logger.info(f"Database '{DATABASE}' created successfully with full schema, indexes, and seed data.")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    force_flag = "--force" in sys.argv
    create_database(force=force_flag)
