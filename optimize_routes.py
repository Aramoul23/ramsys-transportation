"""
Ramsys School Bus Routing System — Route Optimizer
===================================================
Uses Google OR-Tools CVRPTW to assign students to buses and sequence stops.

Improvements vs previous version:
  1. O(n) cycle separation via SetAllowedVehiclesForIndex — was O(kp*mh)
     pairwise constraints which blows up solver time at 100+ families
  2. School arrival deadline (08:30) pinned per-vehicle via CumulVar.SetMax
  3. Hard route-time cap derived from config constants (07:15->08:30 = 75 min)
  4. DB connections protected with try/finally — no connection leaks
  5. extract_and_save wrapped in try/except/rollback (atomic write)
  6. ROAD_FACTOR imported from config (was hardcoded 1.3)
  7. Speed calibration uses median (robust to distance outliers)
  8. allow_sibling_mixing scenario flag now actually enforced
  9. Pre-flight capacity warning before invoking solver
 10. Late-arrival warning when farthest-first reorder pushes past 08:30
"""

import sqlite3
import math
import statistics
from datetime import datetime, timedelta
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from config import (
    SCHOOL_LAT, SCHOOL_LON, SCHOOL_NAME, DATABASE,
    MORNING_DEPARTURE_HOUR, MORNING_DEPARTURE_MINUTE,
    MORNING_DEADLINE_HOUR, MORNING_DEADLINE_MINUTE,
    ROAD_FACTOR,
    DEFAULT_CYCLE_SEPARATION, DEFAULT_MAX_STOPS_PER_BUS,
    DEFAULT_MAX_ROUTE_MINUTES, DEFAULT_SOLVER_TIME_LIMIT_SECS,
    DEFAULT_MIN_BUS_UTILIZATION_PCT,
)

# Physical route time budget: departure -> school bell (minutes)
# 07:15 -> 08:30 = 75 minutes. Admins cannot set a larger window than this.
MAX_ROUTE_MINUTES_HARD = (
    (MORNING_DEADLINE_HOUR * 60 + MORNING_DEADLINE_MINUTE)
    - (MORNING_DEPARTURE_HOUR * 60 + MORNING_DEPARTURE_MINUTE)
)


# ============================================================
# 0. SCENARIO CONFIG
# ============================================================
def get_scenario_config() -> dict:
    """Read optimizer parameters from the database. Falls back to config defaults."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        rows = conn.execute('SELECT key, value FROM scenario_config').fetchall()
        if rows:
            return {r[0]: r[1] for r in rows}
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    return {
        'cycle_separation':          str(DEFAULT_CYCLE_SEPARATION).lower(),
        'allow_sibling_mixing':      'true',
        'max_stops_per_bus':         str(DEFAULT_MAX_STOPS_PER_BUS),
        'max_route_minutes':         str(DEFAULT_MAX_ROUTE_MINUTES),
        'solver_time_limit_seconds': str(DEFAULT_SOLVER_TIME_LIMIT_SECS),
        'min_bus_utilization_pct':   str(DEFAULT_MIN_BUS_UTILIZATION_PCT),
    }


# ============================================================
# 1. DISTANCE UTILITY
# ============================================================
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


# ============================================================
# 2. DATA MODEL
# ============================================================
def create_data_model():
    """
    Load all required data from the database in a single connection
    and build the OR-Tools data structure.
    Returns None if any required data is missing.

    Connection is always closed via try/finally regardless of exceptions.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row

        families = conn.execute('''
            SELECT id, family_name, latitude, longitude, student_count, cycle_profile
            FROM families
            WHERE student_count > 0
            ORDER BY id
        ''').fetchall()

        buses = conn.execute('''
            SELECT id, driver_name, capacity, bus_type
            FROM buses
            WHERE is_active = 1
        ''').fetchall()

        matrix_rows = conn.execute('''
            SELECT family_id, travel_time_seconds
            FROM travel_times_morning
        ''').fetchall()

    finally:
        if conn:
            conn.close()

    if not families:
        print("No families in database. Import students first.")
        return None
    if not buses:
        print("No active buses in database. Add buses via the admin dashboard.")
        return None
    if not matrix_rows:
        print("No travel-time matrix found. Run here_matrix.py first.")
        return None

    family_id_to_time = {r['family_id']: r['travel_time_seconds'] for r in matrix_rows}

    data = {}

    # Node 0 = School (depot)
    locations      = [(SCHOOL_LAT, SCHOOL_LON)]
    demands        = [0]
    family_ids     = [0]
    family_names   = [SCHOOL_NAME]
    cycle_profiles = ['MIXED']

    for f in families:
        locations.append((f['latitude'], f['longitude']))
        demands.append(f['student_count'])
        family_ids.append(f['id'])
        family_names.append(f['family_name'])
        cycle_profiles.append(f['cycle_profile'])

    data['locations']      = locations
    data['demands']        = demands
    data['family_ids']     = family_ids
    data['family_names']   = family_names
    data['cycle_profiles'] = cycle_profiles
    data['vehicle_capacities'] = [b['capacity']    for b in buses]
    data['vehicle_names']      = [b['driver_name'] for b in buses]
    data['vehicle_ids']        = [b['id']          for b in buses]
    data['num_vehicles']       = len(buses)
    data['depot']              = 0

    # ----------------------------------------------------------
    # PRE-FLIGHT CAPACITY CHECK
    # Warn before invoking solver so the admin sees this in the log
    # without waiting for the full solver run to fail.
    # ----------------------------------------------------------
    total_demand   = sum(demands)
    total_capacity = sum(data['vehicle_capacities'])
    if total_demand > total_capacity:
        shortfall  = total_demand - total_capacity
        extra_buses = -(-shortfall // 30)
        print(f"WARNING: Capacity shortfall — {total_demand} students but only "
              f"{total_capacity} seats. Need ~{extra_buses} more bus(es). "
              f"The solver will drop the lowest-penalty students.")

    # ----------------------------------------------------------
    # Build the full NxN time matrix
    # ----------------------------------------------------------
    # Step A: calibrate speed from HERE data using MEDIAN
    # Median is robust to outliers (e.g. one very distant family skewing the mean)
    speeds = []
    for idx in range(1, len(locations)):
        fam_id   = family_ids[idx]
        real_sec = family_id_to_time.get(fam_id)
        if real_sec and real_sec > 60:
            km = haversine_km(
                locations[idx][0], locations[idx][1],
                SCHOOL_LAT, SCHOOL_LON
            )
            if km > 0.3:   # Ignore very short trips — speed estimates are noisy below 300m
                speeds.append(km / (real_sec / 3600))

    if len(speeds) >= 3:
        avg_speed = statistics.median(speeds)
    elif speeds:
        avg_speed = sum(speeds) / len(speeds)
    else:
        avg_speed = 20.0   # fallback: 20 km/h urban

    print(f"  Calibrated speed: {avg_speed:.1f} km/h "
          f"(median of {len(speeds)} HERE points, road factor x{ROAD_FACTOR})")

    # Step B: fill NxN matrix
    matrix = []
    for from_idx in range(len(locations)):
        row = []
        for to_idx in range(len(locations)):
            if from_idx == to_idx:
                row.append(0)
            elif to_idx == 0:
                # Any stop -> School: use real HERE API time
                fid  = family_ids[from_idx]
                secs = family_id_to_time.get(fid, 1800)
                row.append(max(1, int(secs / 60)))
            elif from_idx == 0:
                # School -> any stop (symmetric for morning pickup direction)
                fid  = family_ids[to_idx]
                secs = family_id_to_time.get(fid, 1800)
                row.append(max(1, int(secs / 60)))
            else:
                # Stop -> Stop: haversine * road_factor / calibrated speed
                km   = haversine_km(
                    locations[from_idx][0], locations[from_idx][1],
                    locations[to_idx][0],   locations[to_idx][1]
                )
                mins = max(1, int((km * ROAD_FACTOR / avg_speed) * 60))
                row.append(mins)
        matrix.append(row)

    data['time_matrix'] = matrix
    return data


# ============================================================
# 3. OPTIMIZER CORE
# ============================================================
def build_and_solve(data: dict, scenario: dict, with_cycle_sep: bool):
    """
    Build the OR-Tools routing model and solve it.
    Returns (solution, routing, manager) or (None, None, None).

    Key improvements vs previous version:
    - Route time budget capped at MAX_ROUTE_MINUTES_HARD (07:15->08:30 = 75 min)
    - School arrival deadline enforced per-vehicle via CumulVar.SetMax
    - Cycle separation uses O(n) SetAllowedVehiclesForIndex instead of O(n^2) pairwise
    - allow_sibling_mixing flag actually used
    """
    max_route_mins = int(scenario.get('max_route_minutes', DEFAULT_MAX_ROUTE_MINUTES))
    max_stops      = int(scenario.get('max_stops_per_bus', DEFAULT_MAX_STOPS_PER_BUS))
    solver_secs    = int(scenario.get('solver_time_limit_seconds', DEFAULT_SOLVER_TIME_LIMIT_SECS))
    sibling_mixing = scenario.get('allow_sibling_mixing', 'true').lower() == 'true'

    # Admins cannot set a route time that violates the physical school bell deadline
    max_route_mins = min(max_route_mins, MAX_ROUTE_MINUTES_HARD)

    manager = pywrapcp.RoutingIndexManager(
        len(data['time_matrix']),
        data['num_vehicles'],
        data['depot']
    )
    routing = pywrapcp.RoutingModel(manager)

    # A. Time callback
    def time_callback(from_idx, to_idx):
        return data['time_matrix'][manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # B. Capacity constraint
    def demand_callback(from_idx):
        return data['demands'][manager.IndexToNode(from_idx)]

    demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, data['vehicle_capacities'], True, 'Capacity'
    )

    # C. Time dimension with per-vehicle school arrival deadline
    routing.AddDimension(
        transit_idx,
        10,              # 10-min slack: small buffer for traffic variation
        max_route_mins,
        True,
        'Time'
    )
    time_dim = routing.GetDimensionOrDie('Time')

    # Enforce 08:30 school bell on every vehicle's depot return
    # Without this, the solver only sees a total budget, not a clock deadline.
    for v in range(data['num_vehicles']):
        time_dim.CumulVar(routing.End(v)).SetMax(max_route_mins)

    # D. Allow dropping nodes (last resort — 10M penalty makes it very expensive)
    penalty = 10_000_000
    for node in range(1, len(data['time_matrix'])):
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    # E. Fleet activation cost (prefer fewer, fuller buses over many half-empty ones)
    cost_per_seat = 16_667
    base_driver   = 2_000_000
    for i, cap in enumerate(data['vehicle_capacities']):
        routing.SetFixedCostOfVehicle(base_driver + cap * cost_per_seat, i)

    # Balance workload across buses (prevents one bus doing all the work)
    time_dim.SetGlobalSpanCostCoefficient(100)

    # F. Max stops per bus
    if max_stops > 0:
        def stop_counter(from_idx):
            return 0 if manager.IndexToNode(from_idx) == 0 else 1

        stop_cb_idx = routing.RegisterUnaryTransitCallback(stop_counter)
        routing.AddDimensionWithVehicleCapacity(
            stop_cb_idx, 0, [max_stops] * data['num_vehicles'], True, 'StopCount'
        )

    # G. Cycle separation — O(n) via SetAllowedVehiclesForIndex
    #
    # Previous approach: O(kp * mh) pairwise inequality constraints.
    # With 80 KP and 80 MH families = 6,400 constraints added to the solver.
    # At 200+ families this becomes a serious performance bottleneck.
    #
    # New approach: proportionally partition the fleet into KP-eligible
    # and MH-eligible vehicles, then restrict each node to its pool.
    # This adds exactly 1 constraint per node = O(n).
    #
    if with_cycle_sep:
        kp_nodes = [i for i, cp in enumerate(data['cycle_profiles']) if cp == 'KP']
        mh_nodes = [i for i, cp in enumerate(data['cycle_profiles']) if cp == 'MH']

        if kp_nodes and mh_nodes:
            # Proportional fleet split based on student demand
            kp_demand    = sum(data['demands'][i] for i in kp_nodes)
            total_demand = sum(data['demands'][1:])   # exclude depot node
            n_buses      = data['num_vehicles']
            kp_bus_count = max(1, min(n_buses - 1,
                                      round(n_buses * kp_demand / total_demand)))

            kp_vehicles  = list(range(0, kp_bus_count))
            mh_vehicles  = list(range(kp_bus_count, n_buses))

            # KP-only families -> can only board KP-designated buses
            for node in kp_nodes:
                if node != 0:
                    routing.SetAllowedVehiclesForIndex(
                        kp_vehicles, manager.NodeToIndex(node)
                    )

            # MH-only families -> can only board MH-designated buses
            for node in mh_nodes:
                if node != 0:
                    routing.SetAllowedVehiclesForIndex(
                        mh_vehicles, manager.NodeToIndex(node)
                    )

            # MIXED families (siblings spanning both cycles):
            # If sibling_mixing is ON (default): no restriction — any bus works.
            # If sibling_mixing is OFF: route them with KP buses by default.
            if not sibling_mixing:
                mixed_nodes = [i for i, cp in enumerate(data['cycle_profiles'])
                               if cp == 'MIXED' and i != 0]
                for node in mixed_nodes:
                    routing.SetAllowedVehiclesForIndex(
                        kp_vehicles, manager.NodeToIndex(node)
                    )

    # H. Solve
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    search_params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_params.time_limit.seconds = solver_secs

    solution = routing.SolveWithParameters(search_params)
    if solution:
        return solution, routing, manager
    return None, None, None


# ============================================================
# 4. RESULT EXTRACTION & DB WRITE
# ============================================================
def extract_and_save(solution, routing, manager, data: dict) -> int:
    """
    Parse the solver solution, apply farthest-first greedy reorder,
    calculate real clock ETAs, and persist to route_stops atomically.

    Returns the number of route-stop records saved.

    Late-arrival check: after the farthest-first reorder, if any bus's
    recalculated school arrival exceeds 08:30 AM, a warning is printed.
    This can happen because the reorder uses straight-line distance, not
    the time matrix, and may produce a slightly longer actual route.
    """
    departure       = datetime(2000, 1, 1, MORNING_DEPARTURE_HOUR, MORNING_DEPARTURE_MINUTE)
    school_deadline = datetime(2000, 1, 1, MORNING_DEADLINE_HOUR, MORNING_DEADLINE_MINUTE)

    route_stops_data = []
    buses_used       = 0
    total_time       = 0
    late_buses       = []

    # Define helpers outside the loop — they don't change per vehicle
    def dist_from_school(node):
        lat, lon = data['locations'][node]
        return haversine_km(lat, lon, SCHOOL_LAT, SCHOOL_LON)

    def dist_between(a, b):
        la, loa = data['locations'][a]
        lb, lob = data['locations'][b]
        return haversine_km(la, loa, lb, lob)

    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)

        # Collect OR-Tools assigned nodes for this vehicle
        route_nodes = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                route_nodes.append(node)
            index = solution.Value(routing.NextVar(index))

        if not route_nodes:
            continue

        buses_used += 1

        # --------------------------------------------------
        # FARTHEST-FIRST GREEDY REORDER
        # OR-Tools optimises for total travel time. We re-sequence
        # for student welfare: pick up the farthest student first
        # so they spend the least extra time on the bus waiting.
        # --------------------------------------------------
        remaining = list(route_nodes)
        ordered   = []
        current   = max(remaining, key=dist_from_school)
        remaining.remove(current)
        ordered.append(current)

        while remaining:
            nearest = min(remaining, key=lambda n: dist_between(ordered[-1], n))
            remaining.remove(nearest)
            ordered.append(nearest)

        route_nodes = ordered
        # --------------------------------------------------

        route_load   = 0
        trip_minutes = 0
        stop_seq     = 1
        plan_lines   = [
            f"\nRoute for {data['vehicle_names'][vehicle_id]}"
            f" (capacity: {data['vehicle_capacities'][vehicle_id]})"
        ]

        prev_node = route_nodes[0]

        for n in route_nodes:
            route_load += data['demands'][n]

            if n != route_nodes[0]:
                trip_minutes += data['time_matrix'][prev_node][n]

            eta_dt  = departure + timedelta(minutes=trip_minutes)
            eta_str = eta_dt.strftime("%I:%M %p")

            plan_lines.append(
                f"  [{stop_seq:>2}] {data['family_names'][n]}"
                f" ({data['demands'][n]} students) - ETA {eta_str}"
            )
            route_stops_data.append((
                data['vehicle_ids'][vehicle_id],
                data['family_ids'][n],
                stop_seq,
                eta_str
            ))

            stop_seq  += 1
            prev_node  = n

        # Final leg to school and late-arrival check
        trip_minutes      += data['time_matrix'][prev_node][0]
        school_arrival_dt  = departure + timedelta(minutes=trip_minutes)
        school_arrival_str = school_arrival_dt.strftime("%I:%M %p")

        if school_arrival_dt > school_deadline:
            late_min = int((school_arrival_dt - school_deadline).total_seconds() / 60)
            late_buses.append(
                f"Bus {data['vehicle_ids'][vehicle_id]} "
                f"({data['vehicle_names'][vehicle_id]}): "
                f"arrives {school_arrival_str} - {late_min} min late"
            )
            plan_lines.append(
                f"  LATE: arrives {school_arrival_str} (target 08:30 AM, {late_min} min over)"
            )
        else:
            plan_lines.append(
                f"  Arrives at school: {school_arrival_str} (target 08:30 AM) OK"
            )

        plan_lines.append(
            f"  Load: {route_load}/{data['vehicle_capacities'][vehicle_id]} students"
        )
        print("\n".join(plan_lines))
        total_time += trip_minutes

    print("=" * 50)
    print("OPTIMIZATION COMPLETE")
    print(f"   Buses used       : {buses_used} / {data['num_vehicles']} available")
    print(f"   Total fleet time : {total_time} minutes")
    if late_buses:
        print(f"\n   WARNING: {len(late_buses)} bus(es) may arrive late after reorder:")
        for b in late_buses:
            print(f"     - {b}")
        print("   Fix: increase solver_time_limit_seconds or reduce max_stops_per_bus")
    print("=" * 50)

    # Persist atomically — rollback if insert fails so routes are never half-written
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("DELETE FROM route_stops")
        conn.executemany(
            "INSERT INTO route_stops (bus_id, family_id, stop_sequence, estimated_pickup_time)"
            " VALUES (?, ?, ?, ?)",
            route_stops_data
        )
        conn.commit()
        print(f"Saved {len(route_stops_data)} stops to driver database.")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"ERROR: Failed to save routes — {e}")
        raise
    finally:
        if conn:
            conn.close()

    return len(route_stops_data)


# ============================================================
# 5. MAIN ENTRY POINT
# ============================================================
def main(scenario: dict = None):
    """
    Two-pass cycle separation strategy:
    Pass 1 — hard cycle separation (KP buses != MH buses).
    Pass 2 — if Pass 1 finds no solution, retry without separation and warn.
    """
    if scenario is None:
        scenario = get_scenario_config()

    cycle_sep_on = scenario.get('cycle_separation', 'true').lower() == 'true'
    max_stops    = int(scenario.get('max_stops_per_bus', DEFAULT_MAX_STOPS_PER_BUS))
    max_mins     = min(
        int(scenario.get('max_route_minutes', DEFAULT_MAX_ROUTE_MINUTES)),
        MAX_ROUTE_MINUTES_HARD
    )
    solver_secs  = int(scenario.get('solver_time_limit_seconds', DEFAULT_SOLVER_TIME_LIMIT_SECS))

    print(f"\nSCENARIO SETTINGS:")
    print(f"   Cycle separation : {'ON' if cycle_sep_on else 'OFF'}")
    print(f"   Max stops/bus    : {max_stops}")
    print(f"   Max route time   : {max_mins} min  (hard cap: {MAX_ROUTE_MINUTES_HARD} min)")
    print(f"   Solver time limit: {solver_secs}s")
    print(f"   School           : {SCHOOL_NAME} ({SCHOOL_LAT}, {SCHOOL_LON})\n")

    data = create_data_model()
    if not data:
        return

    total_demand = sum(data['demands'])
    avg_cap = (sum(data['vehicle_capacities']) / len(data['vehicle_capacities'])
               if data['vehicle_capacities'] else 30)
    buses_needed = max(1, -(-total_demand // int(avg_cap * 0.7)))
    print(f"{total_demand} students across {len(data['family_ids'])-1} stops | "
          f"~{buses_needed} buses needed (target >=70% load)\n")

    solution = None
    routing  = None
    manager  = None
    sep_used = False

    if cycle_sep_on:
        print("Pass 1: Solving WITH cycle separation (KP != MH buses)...")
        solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=True)
        if solution:
            sep_used = True
            print("Pass 1 succeeded — cycle separation applied.")
        else:
            print("Pass 1 found no solution (probably not enough buses to separate cycles).")
            print("Pass 2: Retrying WITHOUT cycle separation...")
            solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=False)
            if solution:
                print("Pass 2 succeeded — cycles mixed. Add buses to enable separation.")
    else:
        print("Solving WITHOUT cycle separation (as configured)...")
        solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=False)
        if solution:
            print("Solution found.")

    if solution:
        extract_and_save(solution, routing, manager, data)
        if cycle_sep_on and not sep_used:
            print("\nNOTE: Cycle separation was requested but could not be enforced.")
            print("   Add more buses in the admin dashboard, then re-run the optimizer.")
    else:
        print("\nNo solution found.")
        print("   Possible causes:")
        print("   - Not enough buses for the number of students")
        print("   - max_route_minutes is too tight for these distances")
        print("   - Travel-time cache is stale — run here_matrix.py first")


if __name__ == '__main__':
    main()
