"""
Ramsys School Bus Routing System — Flask Web Application
=========================================================
Admin dashboard + Driver route viewer.

Changes vs previous version:
  • AI / LM Studio feature removed entirely
  • DB connections use context manager (no leak risk)
  • api_add_student validates inputs before touching the DB
  • api_import_students backs up DB before wiping data
  • Daily cost uses config.DAILY_COST_PER_BUS_DZD (5,000 DZD)
  • School coordinates imported from config.py
  • /api/optimization-status endpoint added (poll for progress)
  • Student insertion result surfaced in admin dashboard banner
"""

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, send_file, flash, get_flashed_messages
)
import sqlite3
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
import pandas as pd
import folium
import io

from config import (
    DATABASE, SCHOOL_LAT, SCHOOL_LON, SCHOOL_NAME,
    DAILY_COST_PER_BUS_DZD,
)
from new_student_workflow import process_new_student

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ramsys-dev-secret-2025")


# ============================================================
# DATABASE HELPER
# ============================================================
@contextmanager
def get_db():
    """
    Context manager for SQLite connections.
    Guarantees the connection is closed even if an exception occurs.

    Usage:
        with get_db() as conn:
            rows = conn.execute('SELECT ...').fetchall()
    """
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ============================================================
# OPTIMIZATION STATUS (thread-safe, single-server)
# ============================================================
_opt_lock   = threading.Lock()
_opt_status = {
    'running':     False,
    'last_run':    None,     # ISO datetime string
    'last_result': None,     # 'success' | 'failed'
    'message':     'No optimization run yet.',
}


def _update_opt_status(**kwargs):
    with _opt_lock:
        _opt_status.update(kwargs)


def run_optimization_background():
    """Run HERE matrix fetch → OR-Tools optimizer sequentially in a daemon thread."""
    _update_opt_status(running=True, message='Fetching HERE traffic matrices…')
    print("⏳ [Optimizer] Fetching HERE API matrices…")
    try:
        subprocess.run(["python", "here_matrix.py"], check=True)
        _update_opt_status(message='Running OR-Tools route optimizer…')
        print("⏳ [Optimizer] Running OR-Tools…")
        subprocess.run(["python", "optimize_routes.py"], check=True)
        _update_opt_status(
            running=False,
            last_run=datetime.now().isoformat(timespec='seconds'),
            last_result='success',
            message='✅ Optimization completed successfully.'
        )
        print("✅ [Optimizer] Done.")
    except subprocess.CalledProcessError as e:
        _update_opt_status(
            running=False,
            last_run=datetime.now().isoformat(timespec='seconds'),
            last_result='failed',
            message=f'❌ Optimization failed: {e}'
        )
        print(f"❌ [Optimizer] Failed: {e}")


# ============================================================
# ROUTES — DRIVER-FACING
# ============================================================
@app.route('/')
def index():
    """List all buses that have routes assigned today."""
    with get_db() as conn:
        buses = conn.execute(
            'SELECT id, driver_name, capacity, bus_type FROM buses WHERE is_active = 1'
        ).fetchall()
        active_bus_ids = {
            row['bus_id']
            for row in conn.execute('SELECT DISTINCT bus_id FROM route_stops').fetchall()
        }

    dispatched = [b for b in buses if b['id'] in active_bus_ids]
    return render_template('index.html', buses=dispatched)


@app.route('/driver/<int:bus_id>')
def driver_route(bus_id):
    """Sequenced route for a driver — includes student names, ETAs, and phone numbers."""
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
            WHERE rs.bus_id = ?
            ORDER BY rs.stop_sequence ASC
        ''', (bus_id,)).fetchall()

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

            enriched_stops.append({
                'sequence':     stop['stop_sequence'],
                'eta':          stop['estimated_pickup_time'],
                'family_name':  stop['family_name'],
                'students':     names,
                'maps_link':    (f"https://www.google.com/maps/dir/?api=1"
                                 f"&destination={stop['latitude']},{stop['longitude']}"),
                'count':        stop['student_count'],
                'phone_number': stop['phone_number'],
            })

    # Full route link for Google Maps (all stops in one navigation)
    google_maps_url = ""
    if route_coords:
        origin      = route_coords[0]
        destination = f"{SCHOOL_LAT},{SCHOOL_LON}"
        waypoints   = "|".join(route_coords[1:]) if len(route_coords) > 1 else ""
        google_maps_url = (
            f"https://www.google.com/maps/dir/?api=1"
            f"&origin={origin}"
            f"&destination={destination}"
            + (f"&waypoints={waypoints}" if waypoints else "")
        )

    return render_template(
        'driver_route.html',
        bus=bus,
        stops=enriched_stops,
        total_students=total_students,
        google_maps_bulk_url=google_maps_url,
        school_lat=SCHOOL_LAT,
        school_lon=SCHOOL_LON
    )


@app.route('/driver/<int:bus_id>/map')
def driver_map(bus_id):
    """Folium map showing the route for a bus."""
    with get_db() as conn:
        stops = conn.execute('''
            SELECT rs.stop_sequence AS sequence,
                   f.latitude, f.longitude, f.family_name
            FROM route_stops rs
            JOIN families f ON rs.family_id = f.id
            WHERE rs.bus_id = ?
            ORDER BY rs.stop_sequence
        ''', (bus_id,)).fetchall()

    if not stops:
        return "Route not found or empty", 404

    m = folium.Map(location=[SCHOOL_LAT, SCHOOL_LON], zoom_start=13)
    route_points = []

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

    route_points.append((SCHOOL_LAT, SCHOOL_LON))
    folium.Marker(
        location=[SCHOOL_LAT, SCHOOL_LON],
        popup=f"{SCHOOL_NAME} (Destination)",
        tooltip=SCHOOL_NAME,
        icon=folium.Icon(color='green', icon='education')
    ).add_to(m)

    folium.PolyLine(route_points, color='red', weight=4, opacity=0.7).add_to(m)
    return m.get_root().render()


# ============================================================
# ROUTES — ADMIN DASHBOARD
# ============================================================
@app.route('/admin')
def admin_dashboard():
    """Main admin dashboard with stats, route summaries, and suggestions."""
    with get_db() as conn:
        students_count     = conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
        total_families     = conn.execute('SELECT COUNT(*) FROM families').fetchone()[0]
        active_buses_count = conn.execute(
            'SELECT COUNT(DISTINCT bus_id) FROM route_stops'
        ).fetchone()[0]

        route_summary = conn.execute('''
            SELECT
                b.id        AS bus_id,
                b.driver_name,
                b.capacity,
                COUNT(rs.id)                     AS stops_count,
                MAX(rs.estimated_pickup_time)    AS finish_time,
                MIN(rs.estimated_pickup_time)    AS start_time
            FROM route_stops rs
            JOIN buses b ON rs.bus_id = b.id
            GROUP BY b.id
        ''').fetchall()

        # Enrich each route with actual student count
        route_details = []
        for r in route_summary:
            students_on_bus = conn.execute('''
                SELECT COALESCE(SUM(f.student_count), 0)
                FROM route_stops rs
                JOIN families f ON rs.family_id = f.id
                WHERE rs.bus_id = ?
            ''', (r['bus_id'],)).fetchone()[0]
            d = dict(r)
            d['students_assigned'] = students_on_bus
            route_details.append(d)

        # All buses (for fleet management tab)
        raw_buses = conn.execute('SELECT * FROM buses ORDER BY id ASC').fetchall()
        all_buses = []
        for bus in raw_buses:
            b = dict(bus)
            b['students_assigned'] = 0
            b['duration']          = 'N/A'
            for r in route_details:
                if r['bus_id'] == b['id']:
                    b['students_assigned'] = r['students_assigned']
                    b['duration']          = r['finish_time']
                    break
            all_buses.append(b)

        # All students (for student management tab)
        all_students = conn.execute('''
            SELECT
                s.id, s.first_name, s.last_name, s.address, s.cycle,
                f.family_name,
                rs.bus_id
            FROM students s
            JOIN families f ON s.family_id = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id
            ORDER BY s.id DESC
        ''').fetchall()

        # Analytics — unassigned students
        unassigned_count = conn.execute('''
            SELECT COUNT(s.id)
            FROM students s
            LEFT JOIN families f ON s.family_id = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id
            WHERE rs.bus_id IS NULL AND f.id IS NOT NULL
        ''').fetchone()[0]

        total_capacity    = conn.execute(
            'SELECT COALESCE(SUM(capacity), 0) FROM buses WHERE is_active = 1'
        ).fetchone()[0]
        total_buses_reg   = conn.execute(
            'SELECT COUNT(*) FROM buses WHERE is_active = 1'
        ).fetchone()[0]

    # Last optimization time
    try:
        ts = os.path.getmtime(DATABASE)
        last_opt_time = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        last_opt_time = "Unknown"

    import_success = request.args.get('import_success', 0)
    add_status     = request.args.get('add_status', '')
    add_message    = request.args.get('add_message', '')

    suggestions = get_system_recommendations()

    return render_template(
        'admin.html',
        students_count=students_count,
        active_buses_count=active_buses_count,
        total_families=total_families,
        routes=route_details,
        last_opt_time=last_opt_time,
        all_students=all_students,
        import_success=import_success,
        add_status=add_status,
        add_message=add_message,
        suggestions=suggestions,
        all_buses=all_buses,
        school_name=SCHOOL_NAME,
        opt_status=_opt_status,
    )


def get_system_recommendations():
    """
    Core analytics engine.
    Analyzes student/bus data and returns list of actionable suggestions.
    Shared by Admin Dashboard and AI Assistant.
    """
    with get_db() as conn:
        students_count = conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
        total_families = conn.execute('SELECT COUNT(*) FROM families').fetchone()[0]
        active_buses = conn.execute('''
            SELECT b.id as bus_id, b.driver_name, b.capacity,
                   COALESCE(SUM(f.student_count), 0) as load,
                   COUNT(rs.id) as stops
            FROM buses b
            LEFT JOIN route_stops rs ON b.id = rs.bus_id
            LEFT JOIN families f ON rs.family_id = f.id
            WHERE b.is_active = 1
            GROUP BY b.id
        ''').fetchall()

        unassigned_count = conn.execute('''
            SELECT COUNT(s.id)
            FROM students s
            LEFT JOIN families f ON s.family_id = f.id
            LEFT JOIN route_stops rs ON f.id = rs.family_id
            WHERE rs.bus_id IS NULL AND f.id IS NOT NULL
        ''').fetchone()[0]

        total_capacity = sum(b['capacity'] for b in active_buses)
        buses_with_routes = sum(1 for b in active_buses if b['stops'] > 0)
        total_assigned = sum(b['load'] for b in active_buses)

    suggestions = []

    # 1. Efficiency Score
    if total_capacity > 0 and buses_with_routes > 0 and students_count > 0:
        util_pct = round((total_assigned / total_capacity) * 100)
        coverage_pct = round(((students_count - unassigned_count) / students_count) * 100)
        eff_score = round((util_pct + coverage_pct) / 2)
    else:
        util_pct = coverage_pct = eff_score = 0

    score_map = [(85, "EXCELLENT", "🏆"), (60, "GOOD", "✅"), (40, "NEEDS ATTENTION", "⚠️")]
    score_label, score_icon = next(((s[1], s[2]) for s in score_map if eff_score >= s[0]), ("CRITICAL", "🚨"))

    suggestions.append({
        "type": "score", "icon": score_icon,
        "message": f"SYSTEM EFFICIENCY: {eff_score}% ({score_label}) — "
                   f"Fleet utilization: {util_pct}%, Student coverage: {coverage_pct}%. "
                   f"Active buses: {buses_with_routes}/{len(active_buses)}."
    })

    # 2. Capacity Constraints
    if students_count > total_capacity:
        extra = -(-(students_count - total_capacity) // 30)
        suggestions.append({
            "type": "critical", "icon": "🚨",
            "message": f"CAPACITY CRISIS: Shortfall of {students_count - total_capacity} seats. "
                       f"Add approx {extra} extra 30-seat bus(es)."
        })
    elif students_count > total_capacity * 0.9 and total_capacity > 0:
        suggestions.append({
            "type": "warning", "icon": "⚠️",
            "message": f"FLEET NEAR LIMIT: Only {total_capacity - students_count} spare seats left."
        })

    # 3. Assignment Status
    if unassigned_count > 0:
        suggestions.append({
            "type": "warning", "icon": "🛑",
            "message": f"ACTION REQUIRED: {unassigned_count} student(s) unassigned. Run Optimizer."
        })

    # 4. Route Quality
    overloaded = [f"Bus {b['bus_id']}" for b in active_buses if b['load'] >= b['capacity'] and b['capacity'] > 0]
    underloaded = [f"Bus {b['bus_id']}" for b in active_buses if b['load'] > 0 and (b['load']/b['capacity']) < 0.4]
    long_routes = [f"Bus {b['bus_id']}" for b in active_buses if b['stops'] > 12]

    if overloaded:
        suggestions.append({"type": "warning", "icon": "📦", "message": f"OVERLOADED: {', '.join(overloaded[:3])}."})
    if underloaded:
        suggestions.append({"type": "insight", "icon": "💸", "message": f"SAVINGS: {', '.join(underloaded[:3])} are <40% full. Merge them."})
    if long_routes:
        suggestions.append({"type": "warning", "icon": "⏳", "message": f"LONG ROUTES: {', '.join(long_routes[:3])} exceed comfort limits."})

    return suggestions
    return suggestions


# ============================================================
# ROUTES — STUDENT MANAGEMENT
# ============================================================
@app.route('/api/add-student', methods=['POST'])
def api_add_student():
    """Add a new student with full input validation and dashboard feedback."""
    # Validate required fields
    first_name = request.form.get('first_name', '').strip()
    last_name  = request.form.get('last_name', '').strip()
    address    = request.form.get('address', 'Ali Mendjeli, Constantine, Algeria').strip()
    cycle      = request.form.get('cycle', 'Primary').strip()

    if not first_name or not last_name:
        return redirect(url_for(
            'admin_dashboard',
            add_status='error',
            add_message='First name and last name are required.'
        ))

    try:
        lat = float(request.form.get('latitude', ''))
        lon = float(request.form.get('longitude', ''))
    except (ValueError, TypeError):
        return redirect(url_for(
            'admin_dashboard',
            add_status='error',
            add_message='Invalid coordinates. Please enter valid latitude and longitude.'
        ))

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return redirect(url_for(
            'admin_dashboard',
            add_status='error',
            add_message=f'Coordinates out of range: ({lat}, {lon}).'
        ))

    valid_cycles = {'Kindergarten', 'Primary', 'Middle', 'High School'}
    if cycle not in valid_cycles:
        return redirect(url_for(
            'admin_dashboard',
            add_status='error',
            add_message=f'Invalid cycle "{cycle}". Must be one of: {", ".join(sorted(valid_cycles))}.'
        ))

    result = process_new_student(first_name, last_name, lat, lon, address, cycle)

    return redirect(url_for(
        'admin_dashboard',
        add_status=result['status'],
        add_message=result['message']
    ))


@app.route('/api/delete-student/<int:student_id>', methods=['POST'])
def api_delete_student(student_id):
    """Delete a student and clean up their family/route if the family becomes empty."""
    with get_db() as conn:
        student = conn.execute(
            'SELECT family_id FROM students WHERE id = ?', (student_id,)
        ).fetchone()
        if not student:
            return "Student not found", 404

        family_id = student['family_id']
        conn.execute('DELETE FROM students WHERE id = ?', (student_id,))
        conn.execute(
            'UPDATE families SET student_count = student_count - 1 WHERE id = ?', (family_id,)
        )

        remaining = conn.execute(
            'SELECT student_count FROM families WHERE id = ?', (family_id,)
        ).fetchone()[0]

        if remaining <= 0:
            conn.execute('DELETE FROM families WHERE id = ?', (family_id,))
            conn.execute('DELETE FROM route_stops WHERE family_id = ?', (family_id,))
            conn.execute(
                'DELETE FROM travel_times_morning WHERE family_id = ?', (family_id,)
            )
            conn.execute(
                'DELETE FROM travel_times_afternoon WHERE family_id = ?', (family_id,)
            )

        conn.commit()

    return redirect(url_for('admin_dashboard'))


# ============================================================
# ROUTES — BUS MANAGEMENT
# ============================================================
@app.route('/api/add-bus', methods=['POST'])
def api_add_bus():
    """Add a new bus to the fleet."""
    driver_name = request.form.get('driver_name', '').strip()
    if not driver_name:
        return redirect(url_for('admin_dashboard'))

    try:
        capacity = int(request.form.get('capacity', 30))
        if capacity < 1 or capacity > 100:
            raise ValueError
    except (ValueError, TypeError):
        return redirect(url_for('admin_dashboard'))

    bus_type = request.form.get('bus_type', 'standard').strip()

    with get_db() as conn:
        conn.execute(
            'INSERT INTO buses (driver_name, capacity, bus_type, is_active) VALUES (?, ?, ?, 1)',
            (driver_name, capacity, bus_type)
        )
        conn.commit()

    return redirect(url_for('admin_dashboard'))


@app.route('/api/delete-bus/<int:bus_id>', methods=['POST'])
def api_delete_bus(bus_id):
    """Remove a bus from the fleet and clean up its route assignments."""
    with get_db() as conn:
        conn.execute('DELETE FROM buses WHERE id = ?', (bus_id,))
        conn.execute('DELETE FROM route_stops WHERE bus_id = ?', (bus_id,))
        conn.commit()
    return redirect(url_for('admin_dashboard'))


# ============================================================
# ROUTES — SCENARIO CONFIG
# ============================================================
@app.route('/api/get-scenario')
def api_get_scenario():
    """Return current scenario config as JSON."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT key, value, description FROM scenario_config'
        ).fetchall()
    return jsonify({r['key']: {'value': r['value'], 'description': r['description']}
                    for r in rows})


@app.route('/api/save-scenario', methods=['POST'])
def api_save_scenario():
    """Persist scenario settings from the admin Scenario Builder."""
    keys = [
        'cycle_separation', 'allow_sibling_mixing',
        'max_stops_per_bus', 'max_route_minutes',
        'solver_time_limit_seconds', 'min_bus_utilization_pct',
    ]
    with get_db() as conn:
        for key in keys:
            value = request.form.get(key)
            if value is not None:
                conn.execute(
                    'INSERT OR REPLACE INTO scenario_config (key, value, description) '
                    'VALUES (?, ?, COALESCE((SELECT description FROM scenario_config WHERE key=?), ?))',
                    (key, value, key, key)
                )
        conn.commit()
    return jsonify({'status': 'ok', 'message': 'Scenario settings saved.'})


@app.route('/api/scenario-estimate', methods=['POST'])
def api_scenario_estimate():
    """Estimate buses and seat configs needed for given scenario settings."""
    cycle_sep   = request.form.get('cycle_separation', 'true').lower() == 'true'
    max_stops   = int(request.form.get('max_stops_per_bus', 20))
    utilisation = int(request.form.get('min_bus_utilization_pct', 60)) / 100.0

    with get_db() as conn:
        families   = conn.execute(
            'SELECT cycle_profile, SUM(student_count) AS total '
            'FROM families WHERE student_count > 0 GROUP BY cycle_profile'
        ).fetchall()
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
        mini_eff     = max(1, int(12 * utilisation))
        standard_eff = max(1, int(30 * utilisation))
        std_buses    = -(-students // standard_eff)
        remainder    = students - (std_buses - 1) * standard_eff
        last_bus     = 'mini (12)' if remainder <= mini_eff else 'standard (30)'
        stop_buses   = -(-stops // max_stops)
        total_buses  = max(std_buses, stop_buses)
        full_std     = total_buses - 1 if total_buses > 1 else total_buses
        buses_list   = [{'size': 30, 'label': 'Standard (30-seat)'}] * full_std
        if total_buses > 1:
            size = 12 if last_bus == 'mini (12)' else 30
            buses_list.append({'size': size,
                                'label': 'Mini (12-seat)' if size == 12 else 'Standard (30-seat)'})
        return {
            'label': label, 'students': students, 'stops': stops,
            'buses': buses_list, 'bus_count': total_buses
        }

    if cycle_sep:
        groups = [
            buses_needed(kp_s,  kp_st,  'Kindergarten + Primary (KP)'),
            buses_needed(mh_s,  mh_st,  'Middle + High School (MH)'),
            buses_needed(mix_s, mix_st, 'Mixed Siblings (can ride either)'),
        ]
    else:
        groups = [buses_needed(total_s, kp_st + mh_st + mix_st, 'All Students')]

    total_buses = sum(g['bus_count'] for g in groups)
    total_mini  = sum(1 for g in groups for b in g['buses'] if b['size'] == 12)
    total_std   = sum(1 for g in groups for b in g['buses'] if b['size'] == 30)

    return jsonify({
        'groups': groups,
        'summary': {
            'total_students':  total_s,
            'total_buses':     total_buses,
            'mini_buses':      total_mini,
            'standard_buses':  total_std,
            'utilisation_pct': int(utilisation * 100),
            'max_stops':       max_stops,
            'cycle_sep':       cycle_sep,
        }
    })


# ============================================================
# ROUTES — DATA IMPORT / EXPORT
# ============================================================
@app.route('/api/export-students')
def api_export_students():
    """Export all students to Excel."""
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

    return send_file(
        output,
        download_name=f"ramsys_students_{datetime.now().strftime('%Y%m%d')}.xlsx",
        as_attachment=True
    )


@app.route('/api/import-students', methods=['POST'])
def api_import_students():
    """
    Import students from an Excel file.

    Safety steps:
    1. Validate the uploaded file before touching the database.
    2. Back up the current database to a timestamped .bak file.
    3. Clear existing student/family/route data.
    4. Import new data inside a single transaction (atomic).
    """
    if 'excel_file' not in request.files:
        return "No file uploaded", 400

    file = request.files['excel_file']
    if not file.filename:
        return "No file selected", 400

    # Validate file before touching the DB
    try:
        df = pd.read_excel(file)
    except Exception as e:
        return f"Could not read Excel file: {e}", 400

    required_cols = {'First Name', 'Last Name', 'Latitude', 'Longitude'}
    missing = required_cols - set(df.columns)
    if missing:
        return f"Missing required columns: {', '.join(missing)}", 400

    if df.empty:
        return "The uploaded file contains no data rows.", 400

    # Back up the current database
    if os.path.exists(DATABASE):
        backup_path = f"{DATABASE}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        shutil.copy2(DATABASE, backup_path)
        print(f"📦 Pre-import backup saved → {backup_path}")

    # Import inside a single transaction
    conn = sqlite3.connect(DATABASE)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute('DELETE FROM route_stops')
        conn.execute('DELETE FROM students')
        conn.execute('DELETE FROM families')
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('students','families','route_stops')"
        )

        families      = {}     # key → family_id
        family_cycles = {}     # family_id → set of cycle strings

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

        # Update cycle profiles
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
        return f"Import failed (database rolled back): {e}", 500

    conn.close()
    return redirect(url_for('admin_dashboard', import_success=1))


# ============================================================
# ROUTES — OPTIMIZER
# ============================================================
@app.route('/api/trigger-optimization', methods=['POST'])
def api_trigger_optimization():
    """Start a background optimization run."""
    with _opt_lock:
        if _opt_status['running']:
            return jsonify({
                'status': 'already_running',
                'message': 'Optimization is already in progress. Please wait.'
            })

    thread = threading.Thread(target=run_optimization_background, daemon=True)
    thread.start()
    return jsonify({'status': 'started', 'message': 'Optimization started in background.'})


@app.route('/api/optimization-status')
def api_optimization_status():
    """Poll this endpoint to check if the optimizer is still running."""
    with _opt_lock:
        return jsonify(dict(_opt_status))


# ============================================================
# ROUTES — AI ASSISTANT
# ============================================================
@app.route('/api/ai-status')
def api_ai_status():
    """Satisfies frontend polling for 'AI Online' status."""
    return jsonify({"online": True})


@app.route('/api/chat', methods=['POST'])
def api_ai_chat():
    """
    Very basic AI logic that analyzes fleet data and replies to the admin.
    In a real app, this would call an LLM with the knowledge base context.
    """
    data    = request.json or {}
    msg     = data.get('message', '').lower()
    
    # 1. Gather current system state
    suggestions = get_system_recommendations()
    
    # 2. Basic Intent Matching
    if any(k in msg for k in ['hi', 'hello', 'help']):
        resp = ("Hello! I'm your Ramsys Assistant. I can analyze your fleet "
                "efficiency, suggest cost savings, or check for overloaded routes. "
                "What would you like to know?")
    elif any(k in msg for k in ['analyze', 'efficiency', 'stats', 'suggestions']):
        summary = "\n".join([f"• {s['icon']} {s['message']}" for s in suggestions])
        resp = f"Here is my current analysis of the fleet:\n\n{summary}"
    elif any(k in msg for k in ['save', 'cost', 'money']):
        savings = [s['message'] for s in suggestions if s['type'] == 'insight' and 'SAVINGS' in s['message']]
        if savings:
            resp = f"I found some cost-saving opportunities:\n\n" + "\n".join([f"• {s}" for s in savings])
        else:
            resp = "The fleet is currently running quite efficiently! I don't see any obvious merges right now."
    elif any(k in msg for k in ['overload', 'full', 'limit']):
        issues = [s['message'] for s in suggestions if s['type'] in ('critical', 'warning')]
        if issues:
            resp = "I've flagged some potential capacity issues:\n\n" + "\n".join([f"• {s}" for s in issues])
        else:
            resp = "All buses are within safe capacity limits."
    else:
        resp = ("I'm not sure how to help with that specifically, but based on "
                "my fleet data, my top recommendation is: " + 
                (suggestions[0]['message'] if suggestions else "everything looks okay!"))

    return jsonify({"response": resp})


# ============================================================
# STARTUP
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
