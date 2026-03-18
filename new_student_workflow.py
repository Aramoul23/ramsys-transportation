"""
Ramsys School Bus Routing System — New Student Insertion Workflow
=================================================================
Handles mid-year student additions without running the full optimizer.

Priority order:
  1. Sibling match  -> auto-assign to sibling's bus (if capacity exists)
  2. Nearby stop    -> auto-insert next to the nearest compatible stop within 500m
  3. No match       -> save to DB, flag for next optimizer run

Improvements vs previous version:
  1. Single try/finally wraps the entire function — no connection leak on any
     exception path (was 6 scattered conn.close() calls with no fallback)
  2. phone_number parameter added to process_new_student — new families now get
     a real contact number instead of NULL in the families table
  3. Nearby insertion now checks cycle compatibility — a Kindergarten student
     will NOT be placed on a Middle/High School bus just because it is nearby
  4. Nearby insertion now checks max_stops_per_bus from scenario_config — a bus
     that has hit its stop limit will not receive new insertions
  5. Removed unused cursor variable (conn.execute() was used throughout)
"""

import sqlite3
import math
from config import (
    DATABASE,
    MAX_INSERTION_DISTANCE_METERS,
    SIBLING_DISTANCE_METERS,
    DEFAULT_MAX_STOPS_PER_BUS,
)


# ============================================================
# DISTANCE UTILITY
# ============================================================
def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two GPS points."""
    R    = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a    = (math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ============================================================
# BUS VALIDATION HELPERS
# ============================================================
def check_bus_capacity(conn: sqlite3.Connection, bus_id: int, extra: int = 1) -> bool:
    """Return True if bus_id has at least `extra` free seats."""
    bus = conn.execute("SELECT capacity FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        return False
    capacity = bus[0]
    result = conn.execute('''
        SELECT COALESCE(SUM(f.student_count), 0)
        FROM route_stops rs
        JOIN families f ON rs.family_id = f.id
        WHERE rs.bus_id = ?
    ''', (bus_id,)).fetchone()
    current_load = result[0] if result else 0
    return (current_load + extra) <= capacity


def check_bus_stop_count(conn: sqlite3.Connection, bus_id: int, max_stops: int) -> bool:
    """
    Return True if bus_id has fewer stops than max_stops.
    Prevents mid-year insertions from silently exceeding the scenario stop limit.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM route_stops WHERE bus_id = ?", (bus_id,)
    ).fetchone()[0]
    return count < max_stops


def get_bus_cycle_profile(conn: sqlite3.Connection, bus_id: int) -> str:
    """
    Infer the effective cycle profile of a bus from its currently assigned families.
    Returns 'KP', 'MH', 'MIXED', or 'EMPTY' (bus has no students yet).

    This is used to prevent inserting a KP student onto an MH bus and
    vice-versa, preserving the cycle separation policy mid-year.
    """
    profiles = conn.execute('''
        SELECT DISTINCT f.cycle_profile
        FROM route_stops rs
        JOIN families f ON rs.family_id = f.id
        WHERE rs.bus_id = ?
    ''', (bus_id,)).fetchall()

    if not profiles:
        return 'EMPTY'
    profile_set = {r[0] for r in profiles}
    if profile_set == {'KP'}:
        return 'KP'
    if profile_set == {'MH'}:
        return 'MH'
    return 'MIXED'


def is_cycle_compatible(bus_profile: str, student_cycle: str) -> bool:
    """
    Return True if a student with the given cycle can board a bus
    with the given cycle profile.

    Rules:
      EMPTY bus  — always accepts any student (first student sets the tone)
      MIXED bus  — accepts any student
      KP bus     — only accepts Kindergarten or Primary students
      MH bus     — only accepts Middle or High School students
    """
    if bus_profile in ('EMPTY', 'MIXED'):
        return True
    kp_cycles = {'Kindergarten', 'Primary'}
    mh_cycles = {'Middle', 'High School'}
    if bus_profile == 'KP':
        return student_cycle in kp_cycles
    if bus_profile == 'MH':
        return student_cycle in mh_cycles
    return True


def get_max_stops_from_scenario(conn: sqlite3.Connection) -> int:
    """Read max_stops_per_bus from scenario_config, fall back to config default."""
    row = conn.execute(
        "SELECT value FROM scenario_config WHERE key = 'max_stops_per_bus'"
    ).fetchone()
    if row:
        try:
            return int(row[0])
        except (ValueError, TypeError):
            pass
    return DEFAULT_MAX_STOPS_PER_BUS


# ============================================================
# FAMILY CYCLE PROFILE
# ============================================================
def update_family_cycle_profile(conn: sqlite3.Connection, family_id: int):
    """Recalculate and persist the cycle_profile for a family from its students."""
    cycles = [
        row[0]
        for row in conn.execute(
            "SELECT cycle FROM students WHERE family_id = ? AND cycle IS NOT NULL",
            (family_id,)
        ).fetchall()
    ]
    has_kp = any(c in ('Kindergarten', 'Primary') for c in cycles)
    has_mh = any(c in ('Middle', 'High School') for c in cycles)

    if has_kp and has_mh:
        profile = 'MIXED'
    elif has_kp:
        profile = 'KP'
    elif has_mh:
        profile = 'MH'
    else:
        profile = 'MIXED'

    conn.execute("UPDATE families SET cycle_profile = ? WHERE id = ?", (profile, family_id))


# ============================================================
# MAIN WORKFLOW
# ============================================================
def process_new_student(
    first_name: str,
    last_name:  str,
    lat:        float,
    lon:        float,
    address:    str = 'Ali Mendjeli, Constantine, Algeria',
    cycle:      str = 'Primary',
    phone:      str = '0555-000-000',
) -> dict:
    """
    Add a new student to the database and attempt to assign them to an
    existing bus route without running the full optimizer.

    Returns a status dict:
    {
        'status':              'auto_routed' | 'sibling_added' | 'bus_full' |
                               'no_nearby_bus' | 'not_routed',
        'message':             str,
        'bus_id':              int | None,
        'requires_reoptimize': bool
    }
    """
    last_name  = last_name.strip().upper()
    first_name = first_name.strip()
    phone      = (phone or '0555-000-000').strip()
    if phone in ('nan', 'None', ''):
        phone = '0555-000-000'

    print(f"Processing: {first_name} {last_name} @ ({lat:.4f}, {lon:.4f}) - {cycle}")

    # Single connection for the entire operation.
    # try/finally guarantees it is closed on every exit path including exceptions.
    conn = sqlite3.connect(DATABASE)
    try:
        # ----------------------------------------------------------------
        # STEP 1: Sibling detection
        # Look for an existing family with the same last name within 50m.
        # ----------------------------------------------------------------
        potential_families = conn.execute(
            "SELECT id, family_name, latitude, longitude FROM families WHERE family_name = ?",
            (last_name,)
        ).fetchall()

        matched_family_id = None
        for fam in potential_families:
            f_id, f_name, f_lat, f_lon = fam
            if haversine_meters(lat, lon, f_lat, f_lon) <= SIBLING_DISTANCE_METERS:
                matched_family_id = f_id
                print(f"  Sibling match -> Family ID {f_id}")
                break

        if matched_family_id:
            # Add student to existing family
            conn.execute(
                "INSERT INTO students "
                "(first_name, last_name, original_lat, original_lon, family_id, address, cycle) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (first_name, last_name, lat, lon, matched_family_id, address, cycle)
            )
            conn.execute(
                "UPDATE families SET student_count = student_count + 1 WHERE id = ?",
                (matched_family_id,)
            )
            update_family_cycle_profile(conn, matched_family_id)
            conn.commit()

            route = conn.execute(
                "SELECT bus_id FROM route_stops WHERE family_id = ?",
                (matched_family_id,)
            ).fetchone()

            if route:
                bus_id = route[0]
                # extra=0: student already counted in the family's student_count above
                if check_bus_capacity(conn, bus_id, 0):
                    msg = (f"{first_name} added to Bus {bus_id} "
                           f"(sibling match - no optimizer run needed).")
                    print(f"  {msg}")
                    return {'status': 'sibling_added', 'message': msg,
                            'bus_id': bus_id, 'requires_reoptimize': False}
                else:
                    msg = (f"{first_name} saved, but Bus {bus_id} is now over capacity. "
                           f"Run the optimizer to rebalance.")
                    print(f"  {msg}")
                    return {'status': 'bus_full', 'message': msg,
                            'bus_id': bus_id, 'requires_reoptimize': True}
            else:
                msg = (f"{first_name} saved to existing family, but that family has "
                       f"no active route yet. Run the optimizer to assign them.")
                print(f"  {msg}")
                return {'status': 'not_routed', 'message': msg,
                        'bus_id': None, 'requires_reoptimize': True}

        # ----------------------------------------------------------------
        # STEP 2: Brand-new family — register, then try nearby insertion.
        #
        # Improvements over previous version:
        #   - phone_number is now stored (was always NULL before)
        #   - nearby search checks cycle compatibility (no KP on MH buses)
        #   - nearby search checks max_stops limit from scenario_config
        # ----------------------------------------------------------------
        print(f"  No sibling found. Registering as new family.")
        conn.execute(
            "INSERT INTO families "
            "(family_name, latitude, longitude, student_count, phone_number, cycle_profile) "
            "VALUES (?, ?, ?, 1, ?, 'MIXED')",
            (last_name, lat, lon, phone)
        )
        new_family_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO students "
            "(first_name, last_name, original_lat, original_lon, family_id, address, cycle) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (first_name, last_name, lat, lon, new_family_id, address, cycle)
        )
        update_family_cycle_profile(conn, new_family_id)
        conn.commit()

        # Read the scenario's stop limit so we don't insert into a full route
        max_stops = get_max_stops_from_scenario(conn)

        active_stops = conn.execute('''
            SELECT rs.family_id, rs.bus_id, rs.stop_sequence, f.latitude, f.longitude
            FROM route_stops rs
            JOIN families f ON rs.family_id = f.id
        ''').fetchall()

        best_stop = None
        min_dist  = MAX_INSERTION_DISTANCE_METERS

        for stop in active_stops:
            s_fam_id, s_bus_id, s_seq, s_lat, s_lon = stop
            dist = haversine_meters(lat, lon, s_lat, s_lon)

            if dist >= min_dist:
                continue   # not closer than current best

            if not check_bus_capacity(conn, s_bus_id, 1):
                continue   # bus is full

            if not check_bus_stop_count(conn, s_bus_id, max_stops):
                continue   # bus has hit its stop limit

            bus_profile = get_bus_cycle_profile(conn, s_bus_id)
            if not is_cycle_compatible(bus_profile, cycle):
                continue   # would break cycle separation policy

            min_dist  = dist
            best_stop = stop

        if best_stop:
            s_fam_id, s_bus_id, s_seq, s_lat, s_lon = best_stop
            new_seq = s_seq + 1

            # Shift all subsequent stops to make room
            conn.execute(
                "UPDATE route_stops SET stop_sequence = stop_sequence + 1 "
                "WHERE bus_id = ? AND stop_sequence >= ?",
                (s_bus_id, new_seq)
            )
            conn.execute(
                "INSERT INTO route_stops "
                "(bus_id, family_id, stop_sequence, estimated_pickup_time) "
                "VALUES (?, ?, ?, ?)",
                (s_bus_id, new_family_id, new_seq, "TBD - run optimizer for exact ETA")
            )
            # Invalidate travel-time cache — HERE API must be re-fetched for this family
            conn.execute(
                "DELETE FROM travel_times_morning   WHERE family_id = ?", (new_family_id,)
            )
            conn.execute(
                "DELETE FROM travel_times_afternoon WHERE family_id = ?", (new_family_id,)
            )
            conn.commit()

            msg = (f"{first_name} inserted into Bus {s_bus_id} "
                   f"(nearest compatible stop was {int(min_dist)}m away). "
                   f"Run the optimizer soon to recalculate exact ETAs.")
            print(f"  {msg}")
            return {'status': 'auto_routed', 'message': msg,
                    'bus_id': s_bus_id, 'requires_reoptimize': True}

        # No compatible nearby bus found
        msg = (f"{first_name} saved to database but could not be auto-assigned. "
               f"No compatible bus with free seats was found within "
               f"{MAX_INSERTION_DISTANCE_METERS}m. "
               f"Run the optimizer to assign them.")
        print(f"  {msg}")
        return {'status': 'no_nearby_bus', 'message': msg,
                'bus_id': None, 'requires_reoptimize': True}

    finally:
        conn.close()   # Always executes — the only conn.close() needed


# ============================================================
# CLI USAGE
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 5:
        result = process_new_student(
            first_name=sys.argv[1],
            last_name=sys.argv[2],
            lat=float(sys.argv[3]),
            lon=float(sys.argv[4]),
            cycle=sys.argv[5] if len(sys.argv) > 5 else 'Primary',
            phone=sys.argv[6] if len(sys.argv) > 6 else '0555-000-000',
        )
        print(f"\nResult: {result}")
    else:
        print("Usage: python new_student_workflow.py FirstName LastName Lat Lon [Cycle] [Phone]")
        print("Example: python new_student_workflow.py Younes CHERIF 36.365 6.615 Primary 0555-123-456")
