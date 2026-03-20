"""
Ramsys School Bus Routing System — Database Migration
======================================================
Safely adds missing columns and tables to an existing database
without losing data. Run this after pulling updates that change the schema.

Usage: python migrate_db.py
"""

import sqlite3
import os
import sys
import logging
from datetime import datetime

from config import DATABASE

logger = logging.getLogger(__name__)


def get_table_columns(cursor, table):
    """Return a set of column names for a given table."""
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def table_exists(cursor, table):
    """Check if a table exists in the database."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def migrate():
    """Apply all pending migrations to bring the database up to date."""
    if not os.path.exists(DATABASE):
        logger.error(f"Database '{DATABASE}' not found. Run init_db.py first.")
        return False

    # Backup before migration
    backup = f"{DATABASE}.pre_migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    import shutil
    shutil.copy2(DATABASE, backup)
    logger.info(f"Backup saved → {backup}")

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")

    changes = 0

    # ----------------------------------------------------------
    # 1. Ensure route_stops has 'session' column
    # ----------------------------------------------------------
    if table_exists(cursor, 'route_stops'):
        cols = get_table_columns(cursor, 'route_stops')
        if 'session' not in cols:
            logger.info("Adding 'session' column to route_stops...")
            cursor.execute(
                "ALTER TABLE route_stops ADD COLUMN session TEXT NOT NULL DEFAULT 'morning'"
            )
            changes += 1
            logger.info("  ✅ route_stops.session added")
        else:
            logger.info("  ✓ route_stops.session already exists")
    else:
        logger.warning("  ⚠️ route_stops table not found — run init_db.py to create it")

    # ----------------------------------------------------------
    # 2. Ensure families has 'phone_number' column
    # ----------------------------------------------------------
    if table_exists(cursor, 'families'):
        cols = get_table_columns(cursor, 'families')
        if 'phone_number' not in cols:
            logger.info("Adding 'phone_number' column to families...")
            cursor.execute(
                "ALTER TABLE families ADD COLUMN phone_number TEXT"
            )
            changes += 1
            logger.info("  ✅ families.phone_number added")
        else:
            logger.info("  ✓ families.phone_number already exists")

        if 'cycle_profile' not in cols:
            logger.info("Adding 'cycle_profile' column to families...")
            cursor.execute(
                "ALTER TABLE families ADD COLUMN cycle_profile TEXT DEFAULT 'MIXED'"
            )
            changes += 1
            logger.info("  ✅ families.cycle_profile added")
        else:
            logger.info("  ✓ families.cycle_profile already exists")

        if 'zone' not in cols:
            logger.info("Adding 'zone' column to families...")
            cursor.execute(
                "ALTER TABLE families ADD COLUMN zone TEXT DEFAULT 'Unknown'"
            )
            changes += 1
            logger.info("  ✅ families.zone added")
        else:
            logger.info("  ✓ families.zone already exists")

    # ----------------------------------------------------------
    # 3. Ensure travel_times_morning / afternoon tables exist
    # ----------------------------------------------------------
    for table in ('travel_times_morning', 'travel_times_afternoon'):
        if not table_exists(cursor, table):
            logger.info(f"Creating '{table}' table...")
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS {table} (
                    family_id           INTEGER PRIMARY KEY,
                    travel_time_seconds INTEGER NOT NULL,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (family_id) REFERENCES families (id)
                )
            ''')
            changes += 1
            logger.info(f"  ✅ {table} created")
        else:
            logger.info(f"  ✓ {table} already exists")

    # ----------------------------------------------------------
    # 4. Ensure scenario_config table exists
    # ----------------------------------------------------------
    if not table_exists(cursor, 'scenario_config'):
        logger.info("Creating 'scenario_config' table...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scenario_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                description TEXT
            )
        ''')
        changes += 1
        logger.info("  ✅ scenario_config created")
    else:
        logger.info("  ✓ scenario_config already exists")

    # ----------------------------------------------------------
    # 5. Ensure indexes exist
    # ----------------------------------------------------------
    indexes = [
        ('idx_route_stops_bus', 'route_stops(bus_id)'),
        ('idx_route_stops_family', 'route_stops(family_id)'),
        ('idx_route_stops_session', 'route_stops(session)'),
        ('idx_students_family', 'students(family_id)'),
        ('idx_students_is_active', 'students(is_active)'),
        ('idx_families_name', 'families(family_name)'),
        ('idx_families_active', 'families(student_count)'),
    ]
    for idx_name, idx_def in indexes:
        cursor.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}')

    conn.commit()
    conn.close()

    if changes == 0:
        logger.info("✅ Database is already up to date. No migration needed.")
    else:
        logger.info(f"✅ Migration complete — {changes} change(s) applied.")

    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    migrate()
