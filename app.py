"""
Ramsys School Bus Routing System — Flask Web Application
=========================================================
Admin dashboard + Driver route viewer.

Changes vs previous version (Final Unified Draft):
  • FIXED: Thread-safe handling of optimizer results (prevents NoneType crashes).
  • FIXED: Official Google Maps Directions API URLs implemented natively.
  • FIXED: Scenario estimator math ("80 buses" bug resolved).
  • NEW: Advanced Fleet Utilization metrics added to admin dashboard.
  • NEW: Full Unassigned Students data fetched for the upcoming modal.
  • NEW: Driver route flips Origin/Destination perfectly for Afternoon runs.
"""

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, send_file, flash, get_flashed_messages
)
from functools import wraps
import sqlite3
import os
import shutil
import threading
import logging
import secrets
from contextlib import contextmanager
from datetime import datetime
import pandas as pd
import folium
import io

from config import (
    DATABASE, SCHOOL_LAT, SCHOOL_LON, SCHOOL_NAME,
    DAILY_COST_PER_BUS_DZD,
    ADMIN_USERNAME, ADMIN_PASSWORD,
    AUDIT_SUSPICIOUS_STUDENT_COUNT, AUDIT_SUSPICIOUS_PHONE_COUNT,
    get_db_connection,
)
from new_student_workflow import process_new_student

# Import background tasks directly
from here_matrix import fetch_and_cache_matrices
from optimize_routes import main as run_optimizer

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('ramsys')

app = Flask(__name__)
_secret = os.environ.get("FLASK_SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
app.secret_key = _secret


# ============================================================
# DATABASE HELPER
# ============================================================
@contextmanager
def get_db():
    conn = get_db_connection()
    conn.execute("PRAGMA busy_timeout = 30000") 
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()

def ensure_status_table():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS system_status (
                key TEXT PRIMARY KEY,
                val TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

try:
    ensure_status_table()
except Exception as e:
    logger.warning(f"Could not initialize system_status table: {e}")


# ============================================================
# OPTIMIZATION STATUS
# ============================================================
def _set_opt_status(running: bool, message: str, result: str = ''):
    with get_db() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO system_status (key, val, updated_at) 
            VALUES 
                ('optimizer_running', ?, CURRENT_TIMESTAMP),
                ('optimizer_message', ?, CURRENT_TIMESTAMP),
                ('optimizer_result', ?, CURRENT_TIMESTAMP)
        ''', (str(running).lower(), message, result))
        conn.commit()

def _get_opt_status():
    with get_db() as conn:
        rows = conn.execute("SELECT key, val, updated_at FROM system_status").fetchall()
        if not rows:
            return {'running': False, 'message': 'No optimization run yet.', 'last_result': ''}
        
        status_dict = {row['key']: row['val'] for row in rows}
        is_running = status_dict.get('optimizer_running') == 'true'
        
        if is_running:
            for row in rows:
                if row['key'] == 'optimizer_running':
                    try:
                        updated_time = datetime.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S')
                        if (datetime.utcnow() - updated_time).total_seconds() > 300: 
                            is_running = False
                            status_dict['optimizer_message'] = 'Optimization timed out or crashed. Ready to run again.'
                    except Exception:
                        pass
                    break
        
    return {
        'running': is_running,
        'message': status_dict.get('optimizer_message', 'No optimization run yet.'),
        'last_result': status_dict.get('optimizer_result', '')
    }

def run_optimization_background():
    _set_opt_status(True, 'Fetching HERE traffic matrices…', 'running')
    try:
        matrix_success = fetch_and_cache_matrices()
        if matrix_success is False:
            logger.warning("HERE API Matrix generation reported a failure or fallback.")

        _set_opt_status(True, 'Running OR-Tools morning optimizer…', 'running')
        res_m = run_optimizer(time_period='morning')

        _set_opt_status(True, 'Running OR-Tools afternoon optimizer…', 'running')
        res_a = run_optimizer(time_period='afternoon')

        # SAFELY HANDLE RESPONSE: Prevents crash if optimize_routes returns None
        msg_m = res_m.get('message', 'OK') if isinstance(res_m, dict) else 'OK'
        msg_a = res_a.get('message', 'OK') if isinstance(res_a, dict) else 'OK'

        final_msg = f"✅ Done. Morning: {msg_m} | Afternoon: {msg_a}"
        _set_opt_status(False, final_msg, 'success')
        logger.info(f"✅ [Optimizer] {final_msg}")

    except Exception as e:
        error_msg = f'❌ Optimization failed: {str(e)}'
        _set_opt_status(False, error_msg, 'failed')
        logger.error(f"❌ [Optimizer] Failed: {e}", exc_info=True)


# ============================================================
# AUTHENTICATION
# ============================================================
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return jsonify({"error": "Authentication required"}), 401, {
                'WWW-Authenticate': 'Basic realm="Ramsys Admin"'
            }
        return f(*args, **kwargs)
    return decorated


# ============================================================
# ROUTES — DRIVER-FACING
# ============================================================
@app.route('/')
def index():
    current_hour = datetime.now().hour
    current_session = 'afternoon' if current_hour >= 12 else 'morning'

    with get_db() as conn:
        buses = conn.execute(
            'SELECT id, driver_name, capacity, bus_type FROM buses WHERE is_active = 1'
        ).fetchall()
        active_bus_ids = {
            row['bus_id']
            for row in conn.execute(
                'SELECT DISTINCT bus_id FROM route_stops WHERE session = ?',
                (current_session,)
            ).fetchall()
        }

    dispatched = [b for b in buses if b['id'] in active_bus_ids]
    return render_template('index.html', buses=dispatched)


@app.route('/driver/<int:bus_id>')
def driver_route(bus_id):
    # Allow explicit session toggle via URL (e.g. ?session=afternoon)
    requested_session = request.args.get('session')
    if requested_session in ['morning', 'afternoon']:
        current_session = requested_session
    else:
        current_hour = datetime.now().hour
        current_session = 'afternoon' if current_hour >= 12 else 'morning'

    with get_db() as conn:
        bus = conn.execute('SELECT * FROM buses WHERE id = ?', (bus_id,)).fetchone()
        if not bus:
            return "Bus not found", 404

        stops = conn.execute('''
            SELECT
                rs.stop_sequence,
                rs.estimated_pickup_time,
                f.id   AS family_id,
                f.family_name,
                f.latitude,
                f.longitude,
                f.student_count,
                f.phone_number
            FROM route_stops rs
            JOIN families f ON rs.family_id = f.id
            WHERE rs.bus_id = ? AND rs.session = ?
            ORDER BY rs.stop_sequence ASC
        ''', (bus_id, current_session)).fetchall()

        departure_time = stops[0]['estimated_pickup_time'] if stops else 'N/A'
        dep_row = conn.execute(
            'SELECT departure_time FROM route_stops WHERE bus_id = ? AND session = ? AND departure_time IS NOT NULL LIMIT 1',
            (bus_id, current_session)
        ).fetchone()
        if dep_row and dep_row['departure_time']:
            departure_time = dep_row['departure_time']

        enriched_stops = []
        total_students = 0
        route_coords   = []

        for stop in stops:
            students = conn.execute(
                'SELECT first_name, last_name FROM students WHERE family_id = ?',
                (stop['family_id'],)
            ).fetchall()

            names = [f"{s['first_name']} {s['last_name']}" for s in students]
            total_students += stop['student_count']
            route_coords.append(f"{stop['latitude']},{stop['longitude']}")

            # FIXED: Official Google Maps Search API for single stops
            enriched_stops.append({
                'sequence':     stop['stop_sequence'],
                'eta':          stop['estimated_pickup_time'],
                'family_name':  stop['family_name'],
                'students':     names,
                'lat':          stop['latitude'],
                'lon':          stop['longitude'],
                'maps_link':    f"https://www.google.com/maps/search/?api=1&query={stop['latitude']},{stop['longitude']}",
                'count':        stop['student_count'],
                'phone_number': stop['phone_number'],
            })

    # FIXED: Official Google Maps Directions API with 9-Waypoint safety limit
    google_maps_url = ""
    if route_coords:
        if current_session == 'morning':
            # Morning: First House -> School
            origin      = route_coords[0]
            destination = f"{SCHOOL_LAT},{SCHOOL_LON}"
            middle_stops = route_coords[1:]
        else:
            # Afternoon: School -> Last House
            origin      = f"{SCHOOL_LAT},{SCHOOL_LON}"
            destination = route_coords[-1]
            middle_stops = route_coords[:-1]

        # Google Maps free URL limit is 9 waypoints. 
        # We truncate middle stops so the Destination is never dropped.
        if len(middle_stops) > 9:
            middle_stops = middle_stops[:9]
            
        waypoints = "|".join(middle_stops)

        google_maps_url = (
            f"https://www.google.com/maps/dir/?api=1"
            f"&origin={origin}"
            f"&destination={destination}"
        )
        if waypoints:
            google_maps_url += f"&waypoints={waypoints}"

    return render_template(
        'driver_route.html',
        bus=bus,
        stops=enriched_stops,
        total_students=total_students,
        google_maps_bulk_url=google_maps_url,
        school_lat=SCHOOL_LAT,
        school_lon=SCHOOL_LON,
        current_session=current_session,
        departure_time=departure_time
    )


@app.route('/driver/<int:bus_id>/map')
def driver_map(bus_id):
    requested_session = request.args.get('session')
    if requested_session in ['morning', 'afternoon']:
        current_session = requested_session
    else:
        current_hour = datetime.now().hour
        current_session = 'afternoon' if current_hour >= 12 else 'morning'

    with get_db() as conn:
        stops = conn.execute('''
            SELECT rs.stop_sequence AS sequence,
                   f.latitude, f.longitude, f.family_name
            FROM route_stops rs
            JOIN families f ON rs.family_id = f.id
            WHERE rs.bus_id = ? AND rs.session = ?
            ORDER BY rs.stop_sequence
        ''', (bus_id, current_session)).fetchall()

    if not stops:
        return "Route not found or empty", 404

    m = folium.Map(location=[SCHOOL_LAT, SCHOOL_LON], zoom_start=13)
    route_points = []

    # If afternoon, school is the first point
    if current_session == 'afternoon':
        route_points.append((SCHOOL_LAT, SCHOOL_LON))

    for stop in stops:
        lat, lon = stop['latitude'], stop['longitude']
        route_points.append((lat, lon))

        folium.Marker(
            location=[lat, lon],
            popup=f"Stop {stop['sequence']}: {stop['family_name']}",
            tooltip=f"{stop['sequence']} — {stop['family_name']}",
            icon=folium.Icon(color='blue', icon='info-sign')
        ).add_to(m)

        folium.map.Marker(
            [lat, lon],
            icon=folium.DivIcon(html=(
                f'<div style="font-size:11pt;color:white;background:#2563eb;'
                f'border:2px solid white;border-radius:50%;width:24px;height:24px;'
                f'text-align:center;line-height:20px;font-weight:bold;'
                f'position:relative;right:12px;bottom:32px;">'
                f'{stop["sequence"]}</div>'
            ))
        ).add_to(m)

    # If morning, school is the final point
    if current_session == 'morning':
        route_points.append((SCHOOL_LAT, SCHOOL_LON))

    folium.Marker(
        location=[SCHOOL_LAT, SCHOOL_LON],
        popup=f"{SCHOOL_NAME} (Depot)",
        tooltip=SCHOOL_NAME,
        icon=folium.Icon(color='green', icon='education')
    ).add_to(m)

    folium.PolyLine(route_points, color='red', weight=4, opacity=0.7).add_to(m)
    return m.get_root().render()


# ============================================================
# ROUTES — ADMIN DASHBOARD (auth required)
# ============================================================
@app.route('/admin')
@requires_auth
def admin_dashboard():
    with get_db() as conn:
        students_count     = conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
        total_families     = conn.execute('SELECT COUNT(*) FROM families').fetchone()[0]
        active_buses_count = conn.execute('SELECT COUNT(DISTINCT bus_id) FROM route_stops').fetchone()[0]

        route_summary = conn.execute('''
            SELECT
                b.id        AS bus_id,
                b.driver_name,
                b.capacity,
                rs.session  AS session,
                COUNT(rs.id)                     AS stops_count,
                MAX(rs.estimated_pickup_time)    AS finish_time,
                MIN(rs.estimated_pickup_time)    AS start_time,
                MIN(rs.departure_time)           AS departure_time
            FROM route_stops rs
            JOIN buses b ON rs.bus_id = b.id
            GROUP BY b.id, rs.session
            ORDER BY rs.session, b.id
        ''').fetchall()

        route_details = []
        for r in route_summary:
            students_on_bus = conn.execute('''
                SELECT COALESCE(SUM(f.student_count), 0)
                FROM route_stops rs
                JOIN families f ON rs.family_id = f.id
                WHERE rs.bus_id = ? AND rs.session = ?
            ''', (r['bus_id'], r['session'])).fetchone()[0]
            d = dict(r)
            d['students_assigned'] = students_on_bus
            route_details.append(d)

        # Advanced Fleet Metrics
        total_capacity_active = conn.execute('''
            SELECT SUM(capacity) FROM buses 
            WHERE is_active = 1 AND id IN (SELECT DISTINCT bus_id FROM route_stops)
        ''').fetchone()[0] or 0
        total_students_routed = sum(r['students_assigned'] for r in route_details if r['session'] == 'morning') # Count distinct kids
        fleet_utilization = int((total_students_routed / total_capacity_active * 100)) if total_capacity_active else 0
        empty_seats = total_capacity_active - total_students_routed

        raw_buses = conn.execute('SELECT * FROM buses ORDER BY id ASC').fetchall()
        all_buses = []
        for bus in raw_buses:
            b = dict(bus)
            b['students_assigned'] = 0
            b['duration']          = 'N/A'
            for r in route_details:
                if r['bus_id'] == b['id'] and r['session'] == 'morning':
                    b['students_assigned'] = r['students_assigned']
                    b['duration']          = r['finish_time']
                    break
            all_buses.append(b)

        all_students = conn.execute('''
            SELECT
                s.id, s.first_name, s.last_name, s.address, s.cycle,
                f.family_name, f.phone_number, f.latitude, f.longitude,
                rs.bus_id
            FROM students s
            JOIN families f ON s.family_id = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id AND rs.session = 'morning'
            ORDER BY s.id DESC
        ''').fetchall()

        # FETCH FULL DETAILS FOR UNASSIGNED STUDENTS (For the UI Modal)
        unassigned_query = conn.execute('''
            SELECT s.id, s.first_name, s.last_name, s.address, s.cycle, f.phone_number, f.latitude, f.longitude
            FROM students s
            LEFT JOIN families f ON s.family_id = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id
            WHERE rs.bus_id IS NULL AND f.id IS NOT NULL
        ''').fetchall()
        unassigned_students_list = [dict(r) for r in unassigned_query]
        unassigned_count = len(unassigned_students_list)

    try:
        ts = os.path.getmtime(DATABASE)
        last_opt_time = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        last_opt_time = "Unknown"

    import_success = request.args.get('import_success', 0)
    add_status     = request.args.get('add_status', '')
    add_message    = request.args.get('add_message', '')

    return render_template(
        'admin.html',
        students_count=students_count,
        active_buses_count=active_buses_count,
        total_families=total_families,
        routes=route_details,
        last_opt_time=last_opt_time,
        all_students=all_students,
        unassigned_students=unassigned_students_list, # Passed to Jinja
        unassigned_count=unassigned_count,
        fleet_utilization=fleet_utilization, # Passed to Jinja
        empty_seats=empty_seats,             # Passed to Jinja
        all_buses=all_buses,
        school_name=SCHOOL_NAME,
        import_success=import_success,
        add_status=add_status,
        add_message=add_message,
        opt_status=_get_opt_status(),
    )


# ============================================================
# ROUTES — STUDENT MANAGEMENT
# ============================================================
@app.route('/api/add-student', methods=['POST'])
@requires_auth
def api_add_student():
    first_name = request.form.get('first_name', '').strip()
    last_name  = request.form.get('last_name', '').strip()
    address    = request.form.get('address', 'Ali Mendjeli, Constantine').strip()
    cycle      = request.form.get('cycle', 'Primary').strip()
    phone      = request.form.get('phone', '0555-000-000').strip()

    if not first_name or not last_name:
        return redirect(url_for('admin_dashboard', add_status='error', add_message='Names required.'))

    try:
        lat = float(request.form.get('latitude', ''))
        lon = float(request.form.get('longitude', ''))
    except:
        return redirect(url_for('admin_dashboard', add_status='error', add_message='Invalid coordinates.'))

    result = process_new_student(first_name, last_name, lat, lon, address, cycle, phone)
    return redirect(url_for('admin_dashboard', add_status=result['status'], add_message=result['message']))


@app.route('/api/delete-student/<int:student_id>', methods=['POST'])
@requires_auth
def api_delete_student(student_id):
    with get_db() as conn:
        student = conn.execute('SELECT family_id FROM students WHERE id = ?', (student_id,)).fetchone()
        if not student:
            return "Student not found", 404

        family_id = student['family_id']
        conn.execute('DELETE FROM students WHERE id = ?', (student_id,))
        conn.execute('UPDATE families SET student_count = student_count - 1 WHERE id = ?', (family_id,))

        remaining = conn.execute('SELECT student_count FROM families WHERE id = ?', (family_id,)).fetchone()[0]

        if remaining <= 0:
            conn.execute('DELETE FROM families WHERE id = ?', (family_id,))
            conn.execute('DELETE FROM route_stops WHERE family_id = ?', (family_id,))
            conn.execute('DELETE FROM travel_times_morning WHERE family_id = ?', (family_id,))
            conn.execute('DELETE FROM travel_times_afternoon WHERE family_id = ?', (family_id,))

        conn.commit()
    return redirect(url_for('admin_dashboard'))


# ============================================================
# ROUTES — BUS MANAGEMENT
# ============================================================
@app.route('/api/add-bus', methods=['POST'])
@requires_auth
def api_add_bus():
    driver_name = request.form.get('driver_name', '').strip()
    capacity = int(request.form.get('capacity', 30))
    bus_type = request.form.get('bus_type', 'standard').strip()

    with get_db() as conn:
        conn.execute(
            'INSERT INTO buses (driver_name, capacity, bus_type, is_active) VALUES (?, ?, ?, 1)',
            (driver_name, capacity, bus_type)
        )
        conn.commit()
    return redirect(url_for('admin_dashboard'))


@app.route('/api/delete-bus/<int:bus_id>', methods=['POST'])
@requires_auth
def api_delete_bus(bus_id):
    with get_db() as conn:
        conn.execute('DELETE FROM buses WHERE id = ?', (bus_id,))
        conn.execute('DELETE FROM route_stops WHERE bus_id = ?', (bus_id,))
        conn.commit()
    return redirect(url_for('admin_dashboard'))


# ============================================================
# ROUTES — SCENARIO CONFIG (FIXED MATH)
# ============================================================
@app.route('/api/get-scenario')
@requires_auth
def api_get_scenario():
    with get_db() as conn:
        rows = conn.execute('SELECT key, value, description FROM scenario_config').fetchall()
    return jsonify({r['key']: {'value': r['value'], 'description': r['description']} for r in rows})

@app.route('/api/save-scenario', methods=['POST'])
@requires_auth
def api_save_scenario():
    keys = ['cycle_separation', 'allow_sibling_mixing', 'max_stops_per_bus', 'max_route_minutes', 'solver_time_limit_seconds', 'min_bus_utilization_pct']
    with get_db() as conn:
        for key in keys:
            value = request.form.get(key)
            if value is not None:
                conn.execute(
                    'INSERT OR REPLACE INTO scenario_config (key, value, description) VALUES (?, ?, COALESCE((SELECT description FROM scenario_config WHERE key=?), ?))',
                    (key, value, key, key)
                )
        conn.commit()
    return jsonify({'status': 'ok', 'message': 'Scenario settings saved.'})

@app.route('/api/scenario-estimate', methods=['POST'])
@requires_auth
def api_scenario_estimate():
    """Fixed estimator: Purely based on seats and stops. No broken time math."""
    cycle_sep   = request.form.get('cycle_separation', 'true').lower() == 'true'
    max_stops   = int(request.form.get('max_stops_per_bus', 20))
    utilisation = int(request.form.get('min_bus_utilization_pct', 60)) / 100.0

    with get_db() as conn:
        families   = conn.execute('''
            SELECT f.cycle_profile, SUM(f.student_count) AS total
            FROM families f
            WHERE f.student_count > 0 GROUP BY f.cycle_profile
        ''').fetchall()
        fam_counts = conn.execute(
            'SELECT cycle_profile, COUNT(*) AS fams '
            'FROM families WHERE student_count > 0 GROUP BY cycle_profile'
        ).fetchall()

    students_by = {r['cycle_profile']: r['total'] for r in families}
    stops_by    = {r['cycle_profile']: r['fams']  for r in fam_counts}

    kp_s   = students_by.get('KP', 0)
    mh_s   = students_by.get('MH', 0)
    mix_s  = students_by.get('MIXED', 0)
    total_s = kp_s + mh_s + mix_s
    
    kp_st  = stops_by.get('KP', 0)
    mh_st  = stops_by.get('MH', 0)
    mix_st = stops_by.get('MIXED', 0)

    def buses_needed(students, stops, label):
        if students == 0:
            return {'label': label, 'students': 0, 'stops': 0, 'buses': [], 'bus_count': 0}
        
        standard_eff = max(1, int(30 * utilisation))
        
        # 1. Capacity requirement
        std_buses = -(-students // standard_eff)
        # 2. Stops requirement
        stop_buses = -(-stops // max_stops)
        
        total_buses = max(std_buses, stop_buses)
        
        buses_list = [{'size': 30, 'label': 'Standard (30-seat)'}] * total_buses
        return {
            'label': label, 'students': students, 'stops': stops,
            'buses': buses_list, 'bus_count': total_buses
        }

    if cycle_sep:
        groups = [
            buses_needed(kp_s,  kp_st, 'Kindergarten + Primary (KP)'),
            buses_needed(mh_s,  mh_st, 'Middle + High School (MH)'),
            buses_needed(mix_s, mix_st, 'Mixed Siblings (can ride either)'),
        ]
    else:
        groups = [buses_needed(total_s, kp_st + mh_st + mix_st, 'All Students')]

    total_buses = sum(g['bus_count'] for g in groups)

    return jsonify({
        'groups': groups,
        'summary': {
            'total_students':  total_s,
            'total_buses':     total_buses,
            'standard_buses':  total_buses,
            'mini_buses':      0, # Simplified for realism
            'utilisation_pct': int(utilisation * 100),
        }
    })


# ============================================================
# ROUTES — DATA IMPORT / EXPORT 
# ============================================================
@app.route('/api/export-students')
@requires_auth
def api_export_students():
    with get_db() as conn:
        df = pd.read_sql_query('''
            SELECT
                s.id            AS "Student ID",
                s.first_name    AS "First Name",
                s.last_name     AS "Last Name",
                s.cycle         AS "Cycle",
                s.address       AS "Address",
                f.latitude      AS "Latitude",
                f.longitude     AS "Longitude",
                f.phone_number  AS "Phone Number",
                rs.bus_id       AS "Assigned Bus"
            FROM students s
            LEFT JOIN families f    ON s.family_id  = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id
            ORDER BY s.id
        ''', conn)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Students')
    output.seek(0)

    return send_file(output, download_name=f"ramsys_students_{datetime.now().strftime('%Y%m%d')}.xlsx", as_attachment=True)

@app.route('/api/import-students', methods=['POST'])
@requires_auth
def api_import_students():
    if 'excel_file' not in request.files: return "No file uploaded", 400
    file = request.files['excel_file']
    if not file.filename: return "No file selected", 400

    try: df = pd.read_excel(file)
    except: return "Could not read Excel file", 400

    if os.path.exists(DATABASE):
        shutil.copy2(DATABASE, f"{DATABASE}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")

    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM route_stops')
        conn.execute('DELETE FROM students')
        conn.execute('DELETE FROM families')
        
        families = {}
        family_cycles = {}

        for _, row in df.iterrows():
            last_name  = str(row.get('Last Name',  '')).strip().upper()
            first_name = str(row.get('First Name', '')).strip()
            address    = str(row.get('Address',    'Ali Mendjeli, Constantine')).strip()
            cycle      = str(row.get('Cycle',      'Primary')).strip()
            phone      = str(row.get('Phone Number', '0555-000-000')).strip()

            if address in ('nan', 'None', ''):
                address = 'Ali Mendjeli, Constantine'
            if phone in ('nan', 'None', ''):
                phone = '0555-000-000'

            try:
                lat = float(row.get('Latitude',  0))
                lon = float(row.get('Longitude', 0))
            except (ValueError, TypeError):
                continue

            if not last_name or not first_name or lat == 0 or lon == 0:
                continue

            family_key = f"{last_name}_{round(lat, 4)}_{round(lon, 4)}"

            if family_key not in families:
                conn.execute(
                    "INSERT INTO families "
                    "(family_name, latitude, longitude, student_count, phone_number, cycle_profile) "
                    "VALUES (?, ?, ?, 0, ?, 'MIXED')",
                    (last_name, lat, lon, phone)
                )
                fam_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                families[family_key] = fam_id
                family_cycles[fam_id] = set()

            fam_id = families[family_key]
            family_cycles[fam_id].add(cycle)

            conn.execute(
                "INSERT INTO students "
                "(first_name, last_name, family_id, original_lat, original_lon, address, cycle) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (first_name, last_name, fam_id, lat, lon, address, cycle)
            )
            conn.execute(
                'UPDATE families SET student_count = student_count + 1 WHERE id = ?',
                (fam_id,)
            )

        for fam_id, cycles in family_cycles.items():
            has_kp = ('Kindergarten' in cycles) or ('Primary' in cycles)
            has_mh = ('Middle' in cycles) or ('High School' in cycles)
            if has_kp and has_mh:
                profile = 'MIXED'
            elif has_kp:
                profile = 'KP'
            elif has_mh:
                profile = 'MH'
            else:
                profile = 'MIXED'
            conn.execute(
                'UPDATE families SET cycle_profile = ? WHERE id = ?', (profile, fam_id)
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return f"Import failed: {e}", 500

    conn.close()
    return redirect(url_for('admin_dashboard', import_success=1))


# ============================================================
# ROUTES — OPTIMIZER (auth required)
# ============================================================
@app.route('/api/trigger-optimization', methods=['POST'])
@requires_auth
def api_trigger_optimization():
    """Start a background optimization run natively."""
    current_status = _get_opt_status()
    if current_status['running']:
        return jsonify({
            'status': 'already_running',
            'message': 'Optimization is already in progress. Please wait.'
        })

    thread = threading.Thread(target=run_optimization_background, daemon=True)
    thread.start()
    return jsonify({'status': 'started', 'message': 'Optimization started in background.'})


@app.route('/api/optimization-status')
@requires_auth
def api_optimization_status():
    """Poll this endpoint to check if the optimizer is still running."""
    return jsonify(_get_opt_status())


# ============================================================
# STARTUP
# ============================================================
if __name__ == '__main__':
    logger.info(f"Starting Ramsys School Bus Routing System")
    logger.info(f"School: {SCHOOL_NAME} ({SCHOOL_LAT}, {SCHOOL_LON})")
    app.run(host='0.0.0.0', port=5000, debug=False)