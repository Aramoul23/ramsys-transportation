"""
Ramsys School Bus Routing System — Route Optimizer
===================================================
Uses Google OR-Tools CVRPTW to assign students to buses and sequence stops.

Production Upgrades:
  • NxN Matrix Integration: Uses exact OSRM road times for Home-to-Home sequencing.
  • Soft Time Windows: Prefers a slightly late bus over dropping a student entirely.
  • Deleted "Smart Reorder" Hack: Relies purely on the mathematically perfect OR-Tools sequence.
  • Thread-safe DB Connections & Atomic Writes.
"""

import sqlite3
import math
import statistics
import logging
from datetime import datetime, timedelta
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from config import (
    SCHOOL_LAT, SCHOOL_LON, SCHOOL_NAME, DATABASE,
    MORNING_DEPARTURE_HOUR, MORNING_DEPARTURE_MINUTE,
    MORNING_DEADLINE_HOUR, MORNING_DEADLINE_MINUTE,
    AFTERNOON_DEPARTURE_HOUR, AFTERNOON_DEPARTURE_MINUTE,
    AFTERNOON_DEADLINE_HOUR, AFTERNOON_DEADLINE_MINUTE,
    ROAD_FACTOR, DEFAULT_CYCLE_SEPARATION, DEFAULT_MAX_STOPS_PER_BUS,
    DEFAULT_MAX_ROUTE_MINUTES, DEFAULT_SOLVER_TIME_LIMIT_SECS,
    DEFAULT_MIN_BUS_UTILIZATION_PCT, get_db_connection,
)

logger = logging.getLogger(__name__)

# --- OPTIMIZER TUNING CONSTANTS ---
PENALTY_DROPPED_NODE     = 10_000_000  
PENALTY_LATE_MINUTE      = 100_000     
COST_BASE_BUS_ACTIVATION = 2_000_000   
COST_PER_BUS_SEAT        = 16_667      
COST_GLOBAL_SPAN         = 100         

MAX_ROUTE_MINUTES_HARD = (
    (MORNING_DEADLINE_HOUR * 60 + MORNING_DEADLINE_MINUTE)
    - (MORNING_DEPARTURE_HOUR * 60 + MORNING_DEPARTURE_MINUTE)
)
MAX_AFTERNOON_ROUTE_MINUTES = (
    (AFTERNOON_DEADLINE_HOUR * 60 + AFTERNOON_DEADLINE_MINUTE)
    - (AFTERNOON_DEPARTURE_HOUR * 60 + AFTERNOON_DEPARTURE_MINUTE)
)
ABSOLUTE_MAX_TIME_ALLOWED = max(MAX_ROUTE_MINUTES_HARD, MAX_AFTERNOON_ROUTE_MINUTES) + 45


def get_scenario_config() -> dict:
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT key, value FROM scenario_config').fetchall()
        if rows:
            return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.warning(f"Could not load scenario config, using defaults: {e}")
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

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def create_data_model(time_period='morning'):
    conn = None
    try:
        conn = get_db_connection()

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

        morning_rows = conn.execute("SELECT family_id, travel_time_seconds FROM travel_times_morning").fetchall()
        afternoon_rows = conn.execute("SELECT family_id, travel_time_seconds FROM travel_times_afternoon").fetchall()
        
        try:
            nxn_rows = conn.execute("SELECT from_id, to_id, travel_time_seconds FROM travel_times_nxn").fetchall()
        except sqlite3.OperationalError:
            logger.warning("travel_times_nxn table missing! Run here_matrix.py first. Falling back to Haversine.")
            nxn_rows = []

    except Exception as e:
        logger.error(f"Failed to load data model: {e}")
        return None
    finally:
        if conn:
            conn.close()

    if not families:
        logger.error("No families in database. Import students first.")
        return None
    if not buses:
        logger.error("No active buses in database. Add buses via the admin dashboard.")
        return None
    if not morning_rows and not afternoon_rows:
        logger.error("No travel-time matrices found. Run here_matrix.py first.")
        return None

    m_times = {r['family_id']: r['travel_time_seconds'] for r in morning_rows}
    a_times = {r['family_id']: r['travel_time_seconds'] for r in afternoon_rows}
    nxn_times = {(r['from_id'], r['to_id']): r['travel_time_seconds'] for r in nxn_rows}

    data = {}
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
    data['time_period']        = time_period

    total_demand   = sum(demands)
    total_capacity = sum(data['vehicle_capacities'])
    if total_demand > total_capacity:
        extra_buses = -(-(total_demand - total_capacity) // 30)
        logger.warning(f"Capacity shortfall — Need ~{extra_buses} more bus(es). Solver will drop students.")

    speeds = []
    for idx in range(1, len(locations)):
        fam_id   = family_ids[idx]
        real_sec = m_times.get(fam_id) or a_times.get(fam_id)
        if real_sec and real_sec > 60:
            km = haversine_km(locations[idx][0], locations[idx][1], SCHOOL_LAT, SCHOOL_LON)
            if km > 0.3:
                speeds.append(km / (real_sec / 3600))

    avg_speed = statistics.median(speeds) if len(speeds) >= 3 else (sum(speeds)/len(speeds) if speeds else 20.0)
    logger.info(f"Calibrated fallback speed: {avg_speed:.1f} km/h")

    matrix = []
    for from_idx in range(len(locations)):
        row = []
        for to_idx in range(len(locations)):
            if from_idx == to_idx:
                row.append(0)
                continue

            f_id = family_ids[from_idx]
            t_id = family_ids[to_idx]
            
            secs = None
            if f_id == 0:
                secs = a_times.get(t_id) 
            elif t_id == 0:
                secs = m_times.get(f_id) 
            else:
                secs = nxn_times.get((f_id, t_id))
                if secs is None:
                    secs = nxn_times.get((t_id, f_id)) 

            if secs is not None:
                row.append(max(1, int(secs / 60)))
            else:
                km = haversine_km(
                    locations[from_idx][0], locations[from_idx][1],
                    locations[to_idx][0],   locations[to_idx][1]
                )
                mins = max(1, int((km * ROAD_FACTOR / avg_speed) * 60))
                row.append(mins)
        matrix.append(row)

    data['time_matrix'] = matrix
    return data

def build_and_solve(data: dict, scenario: dict, with_cycle_sep: bool):
    max_route_mins = int(scenario.get('max_route_minutes', DEFAULT_MAX_ROUTE_MINUTES))
    max_stops      = int(scenario.get('max_stops_per_bus', DEFAULT_MAX_STOPS_PER_BUS))
    solver_secs    = int(scenario.get('solver_time_limit_seconds', DEFAULT_SOLVER_TIME_LIMIT_SECS))
    sibling_mixing = scenario.get('allow_sibling_mixing', 'true').lower() == 'true'

    time_period = data.get('time_period', 'morning')
    target_max_mins = min(max_route_mins, MAX_AFTERNOON_ROUTE_MINUTES if time_period == 'afternoon' else MAX_ROUTE_MINUTES_HARD)

    manager = pywrapcp.RoutingIndexManager(
        len(data['time_matrix']),
        data['num_vehicles'],
        data['depot']
    )
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_idx, to_idx):
        return data['time_matrix'][manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    def demand_callback(from_idx):
        return data['demands'][manager.IndexToNode(from_idx)]

    demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, data['vehicle_capacities'], True, 'Capacity'
    )

    routing.AddDimension(
        transit_idx,
        10,                         
        ABSOLUTE_MAX_TIME_ALLOWED,  
        True,
        'Time'
    )
    time_dim = routing.GetDimensionOrDie('Time')

    for v in range(data['num_vehicles']):
        time_dim.SetCumulVarSoftUpperBound(routing.End(v), target_max_mins, PENALTY_LATE_MINUTE)

    for node in range(1, len(data['time_matrix'])):
        routing.AddDisjunction([manager.NodeToIndex(node)], PENALTY_DROPPED_NODE)

    for i, cap in enumerate(data['vehicle_capacities']):
        routing.SetFixedCostOfVehicle(COST_BASE_BUS_ACTIVATION + (cap * COST_PER_BUS_SEAT), i)

    time_dim.SetGlobalSpanCostCoefficient(COST_GLOBAL_SPAN)

    if max_stops > 0:
        def stop_counter(from_idx):
            return 0 if manager.IndexToNode(from_idx) == 0 else 1

        stop_cb_idx = routing.RegisterUnaryTransitCallback(stop_counter)
        routing.AddDimensionWithVehicleCapacity(
            stop_cb_idx, 0, [max_stops] * data['num_vehicles'], True, 'StopCount'
        )

    if with_cycle_sep:
        try:
            kp_nodes = [i for i, cp in enumerate(data['cycle_profiles']) if cp == 'KP']
            mh_nodes = [i for i, cp in enumerate(data['cycle_profiles']) if cp == 'MH']

            if kp_nodes and mh_nodes:
                kp_demand    = sum(data['demands'][i] for i in kp_nodes)
                total_demand = sum(data['demands'][1:])
                n_buses      = data['num_vehicles']
                kp_bus_count = max(1, min(n_buses - 1, round(n_buses * kp_demand / total_demand)))

                kp_vehicles  = list(range(0, kp_bus_count))
                mh_vehicles  = list(range(kp_bus_count, n_buses))

                for node in kp_nodes:
                    if node != 0:
                        try: routing.SetAllowedVehiclesForIndex(kp_vehicles, manager.NodeToIndex(node))
                        except Exception: pass

                for node in mh_nodes:
                    if node != 0:
                        try: routing.SetAllowedVehiclesForIndex(mh_vehicles, manager.NodeToIndex(node))
                        except Exception: pass

                if not sibling_mixing:
                    mixed_nodes = [i for i, cp in enumerate(data['cycle_profiles']) if cp == 'MIXED' and i != 0]
                    for node in mixed_nodes:
                        try: routing.SetAllowedVehiclesForIndex(kp_vehicles, manager.NodeToIndex(node))
                        except Exception: pass
        except Exception as e:
            logger.warning(f"Cycle separation skipped: {e}")

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.seconds = solver_secs

    solution = routing.SolveWithParameters(search_params)
    if solution:
        return solution, routing, manager
    return None, None, None

def extract_and_save(solution, routing, manager, data: dict) -> int:
    time_period = data.get('time_period', 'morning')

    if time_period == 'afternoon':
        global_departure = datetime(2000, 1, 1, AFTERNOON_DEPARTURE_HOUR, AFTERNOON_DEPARTURE_MINUTE)
        deadline         = datetime(2000, 1, 1, AFTERNOON_DEADLINE_HOUR, AFTERNOON_DEADLINE_MINUTE)
        earliest_allowed = global_departure
    else:
        global_departure = datetime(2000, 1, 1, MORNING_DEPARTURE_HOUR, MORNING_DEPARTURE_MINUTE)
        deadline         = datetime(2000, 1, 1, MORNING_DEADLINE_HOUR, MORNING_DEADLINE_MINUTE)
        earliest_allowed = global_departure

    route_stops_data = []
    buses_used       = 0
    late_buses       = []

    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)
        route_nodes = []
        
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                route_nodes.append(node)
            index = solution.Value(routing.NextVar(index))

        if not route_nodes:
            continue

        buses_used += 1

        if time_period == 'morning':
            route_total_minutes = 0
            temp_prev = data['depot']
            for n in route_nodes:
                route_total_minutes += data['time_matrix'][temp_prev][n]
                temp_prev = n
            route_total_minutes += data['time_matrix'][temp_prev][data['depot']]
            
            computed_departure = deadline - timedelta(minutes=route_total_minutes)
            bus_departure = max(computed_departure, earliest_allowed)
        else:
            bus_departure = global_departure

        bus_departure_str = bus_departure.strftime('%I:%M %p')
        trip_minutes = 0
        stop_seq     = 1
        prev_node = data['depot']
        
        for n in route_nodes:
            trip_minutes += data['time_matrix'][prev_node][n]
            eta_dt  = bus_departure + timedelta(minutes=trip_minutes)
            eta_str = eta_dt.strftime("%I:%M %p")

            route_stops_data.append((
                data['vehicle_ids'][vehicle_id],
                data['family_ids'][n],
                stop_seq,
                eta_str,
                time_period,
                bus_departure_str
            ))
            stop_seq  += 1
            prev_node  = n

        trip_minutes += data['time_matrix'][prev_node][data['depot']]
        arrival_dt   = bus_departure + timedelta(minutes=trip_minutes)
        
        if arrival_dt > deadline:
            late_min = int((arrival_dt - deadline).total_seconds() / 60)
            late_buses.append(f"Bus {data['vehicle_ids'][vehicle_id]} ({late_min} min late)")

    if late_buses:
        logger.warning(f"Soft Time Windows triggered. Late arrivals: {', '.join(late_buses)}")

    conn = None
    try:
        conn = get_db_connection()
        conn.execute("BEGIN TRANSACTION")
        conn.execute("DELETE FROM route_stops WHERE session = ?", (time_period,))
        conn.executemany(
            "INSERT INTO route_stops (bus_id, family_id, stop_sequence, estimated_pickup_time, session, departure_time)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            route_stops_data
        )
        conn.commit()
        logger.info(f"Successfully saved {len(route_stops_data)} stops to the database.")
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Critical error: Failed to save routes. Changes rolled back. Error: {e}")
        raise
    finally:
        if conn: conn.close()

    return len(route_stops_data)

def main(scenario: dict = None, time_period: str = 'morning'):
    if scenario is None:
        scenario = get_scenario_config()

    cycle_sep_on = scenario.get('cycle_separation', 'true').lower() == 'true'

    data = create_data_model(time_period=time_period)
    if not data:
        return {'success': False, 'message': 'Data model creation failed.'}

    solution = None
    routing  = None
    manager  = None
    sep_used = False

    if cycle_sep_on:
        logger.info("Pass 1: Solving WITH cycle separation (KP != MH buses)...")
        solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=True)
        if solution:
            sep_used = True
            logger.info("Pass 1 succeeded — cycle separation applied.")
        else:
            logger.warning("Pass 1 found no solution. Retrying WITHOUT cycle separation...")
            solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=False)
            if solution:
                logger.info("Pass 2 succeeded — cycles mixed. Add buses to enable separation.")
    else:
        logger.info("Solving WITHOUT cycle separation (as configured)...")
        solution, routing, manager = build_and_solve(data, scenario, with_cycle_sep=False)
        if solution:
            logger.info("Solution found.")

    status = {
        'success': False,
        'sep_requested': cycle_sep_on,
        'sep_enforced': False,
        'stops_saved': 0,
        'message': ''
    }

    if solution:
        stops_count = extract_and_save(solution, routing, manager, data)
        status['success'] = True
        status['sep_enforced'] = sep_used
        status['stops_saved'] = stops_count
        if cycle_sep_on and not sep_used:
            status['message'] = "⚠️ Optimized with mixed cycles (strict separation failed)."
        else:
            status['message'] = "✅ Optimization successful."
    else:
        status['message'] = "❌ No solution found."
        logger.error(
            "No solution found. Possible causes:\n"
            "   - Not enough buses for the number of students\n"
            "   - max_route_minutes is too tight for these distances\n"
            "   - Travel-time cache is empty — run here_matrix.py first"
        )

    return status

if __name__ == '__main__':
    import sys
    period = sys.argv[1] if len(sys.argv) > 1 else 'morning'
    main(time_period=period)