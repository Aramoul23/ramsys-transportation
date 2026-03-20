"""
Ramsys School Bus Routing System — OSRM Matrix Fetcher
======================================================
Fetches real-world driving times using the OSRM routing engine.
Now includes batching (chunking) to bypass the 100-coordinate limit
on public OSRM servers, and populates all three required tables.
"""

import sqlite3
import logging
import requests
import time
from config import get_db_connection, SCHOOL_LAT, SCHOOL_LON

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Public OSRM Table API endpoint
OSRM_BASE_URL = "http://router.project-osrm.org/table/v1/driving/"
MAX_PER_BATCH = 70  # Keep safely under the 100 limit

def setup_tables():
    """Creates and clears the exact tables the Optimizer needs."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("PRAGMA busy_timeout = 30000")
        
        # Create tables if they are missing
        conn.execute('''CREATE TABLE IF NOT EXISTS travel_times_morning (family_id INTEGER PRIMARY KEY, travel_time_seconds REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS travel_times_afternoon (family_id INTEGER PRIMARY KEY, travel_time_seconds REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS travel_times_nxn (from_id INTEGER, to_id INTEGER, travel_time_seconds REAL, PRIMARY KEY (from_id, to_id))''')
        
        # Clear old data to ensure a fresh matrix
        conn.execute('DELETE FROM travel_times_morning')
        conn.execute('DELETE FROM travel_times_afternoon')
        conn.execute('DELETE FROM travel_times_nxn')
        
        conn.commit()
        logger.info("Prepared all travel_time tables successfully.")
    except Exception as e:
        logger.error(f"Database error during table setup: {e}")
    finally:
        if conn:
            conn.close()

def fetch_and_cache_matrices(locations_override=None):
    """
    Main function called by app.py. 
    Fetches the travel times in chunks of 70 to respect OSRM limits.
    """
    logger.info("Starting OSRM Matrix fetch with chunking...")
    setup_tables()
    
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("PRAGMA busy_timeout = 30000")
        
        # Gather all families
        families = conn.execute('SELECT id, latitude, longitude FROM families WHERE student_count > 0 ORDER BY id').fetchall()
        
        if not families:
            logger.warning("No families found in database to map.")
            return False
            
        logger.info(f"Loaded {len(families)} families. Processing in batches of {MAX_PER_BATCH}...")

        morning_data = []
        afternoon_data = []
        nxn_data = []

        # Process in chunks
        for i in range(0, len(families), MAX_PER_BATCH):
            chunk = families[i:i + MAX_PER_BATCH]
            
            # Index 0 is ALWAYS the School
            nodes = [(0, SCHOOL_LAT, SCHOOL_LON)]
            for f in chunk:
                nodes.append((f['id'], f['latitude'], f['longitude']))
                
            # OSRM strictly requires lon,lat order
            coord_strings = [f"{lon},{lat}" for _, lat, lon in nodes]
            coords_param = ";".join(coord_strings)
            
            request_url = f"{OSRM_BASE_URL}{coords_param}?annotations=duration"
            logger.info(f"Requesting batch {i // MAX_PER_BATCH + 1} ({len(nodes)} points)...")
            
            response = requests.get(request_url)
            
            if response.status_code != 200:
                logger.error(f"OSRM API failed on a batch: {response.text}")
                continue
                
            data = response.json()
            if data.get('code') != 'Ok':
                logger.error(f"OSRM returned non-Ok code: {data.get('code')}")
                continue
                
            durations = data.get('durations', [])
            
            # Parse the Matrix
            for r_idx, row in enumerate(durations):
                for c_idx, duration_sec in enumerate(row):
                    if r_idx == c_idx or duration_sec is None:
                        continue # Skip self-routes and failed routes
                        
                    from_id = nodes[r_idx][0]
                    to_id = nodes[c_idx][0]
                    
                    if from_id == 0:
                        # School to Home = Afternoon
                        afternoon_data.append((to_id, float(duration_sec)))
                    elif to_id == 0:
                        # Home to School = Morning
                        morning_data.append((from_id, float(duration_sec)))
                    else:
                        # Home to Home = NxN
                        nxn_data.append((from_id, to_id, float(duration_sec)))
            
            # Be polite to the free public server to avoid IP bans
            time.sleep(1.5)

        # Atomic database insert
        conn.execute("BEGIN TRANSACTION")
        conn.executemany("INSERT INTO travel_times_morning (family_id, travel_time_seconds) VALUES (?, ?)", morning_data)
        conn.executemany("INSERT INTO travel_times_afternoon (family_id, travel_time_seconds) VALUES (?, ?)", afternoon_data)
        conn.executemany("INSERT INTO travel_times_nxn (from_id, to_id, travel_time_seconds) VALUES (?, ?, ?)", nxn_data)
        conn.commit()
        
        logger.info(f"Successfully cached {len(morning_data)} Morning, {len(afternoon_data)} Afternoon, and {len(nxn_data)} NxN times!")
        return True

    except Exception as e:
        logger.error(f"Critical error during matrix fetch: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # Allows you to run this file directly from the terminal to build the cache
    fetch_and_cache_matrices()