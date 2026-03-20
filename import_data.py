import pandas as pd
import sqlite3
from geopy.distance import geodesic

SIBLING_DISTANCE_THRESHOLD_METERS = 50

def import_students_and_detect_siblings(excel_file='sample_students.xlsx', db_file='ramsys_routing.db'):
    print(f"Reading student data from {excel_file}...")
    try:
        df = pd.read_excel(excel_file)
    except FileNotFoundError:
        print(f"❌ Error: Could not find {excel_file}.")
        return

    # Check required columns exist
    required_cols = ['first_name', 'last_name', 'latitude', 'longitude', 'zone_label']
    for col in required_cols:
        if col not in df.columns:
            print(f"❌ Error: Missing required column '{col}' in Excel file.")
            return

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    # Clear existing data (for testing purposes, ensures a clean run)
    cursor.execute('DELETE FROM students')
    cursor.execute('DELETE FROM families')
    cursor.execute('DELETE FROM sqlite_sequence WHERE name IN ("students", "families")')
    conn.commit()

    print("Detecting siblings and grouping into families...")
    
    # We will keep track of which families we've created
    # Structure: { last_name: [ { 'id': family_id, 'lat': lat, 'lon': lon, 'count': num_kids, 'zone': zone } ] }
    families_dict = {}
    
    student_insert_records = []
    
    for _, row in df.iterrows():
        first_name = str(row['first_name']).strip()
        last_name = str(row['last_name']).strip().upper()  # Normalize upper case for exact matching
        lat = row['latitude']
        lon = row['longitude']
        zone = str(row['zone_label'])
        
        # Safety check: Ignore students with bad GPS data (0,0)
        if pd.isna(lat) or pd.isna(lon) or (lat == 0 and lon == 0):
            print(f"⚠️ Warning: Skipping {first_name} {last_name} due to invalid GPS data: {lat},{lon}")
            continue

        student_coords = (lat, lon)
        assigned_family_id = None
        
        # 1. Does this last name already exist in our known families?
        if last_name in families_dict:
            # 2. Check if the GPS is within 50 meters of an existing family with the same last name
            for existing_family in families_dict[last_name]:
                existing_coords = (existing_family['lat'], existing_family['lon'])
                distance_meters = geodesic(student_coords, existing_coords).meters
                
                if distance_meters <= SIBLING_DISTANCE_THRESHOLD_METERS:
                    # ✅ Sibling match found!
                    assigned_family_id = existing_family['id']
                    
                    # Update the family centroid (average the GPS coordinates)
                    old_count = existing_family['count']
                    new_count = old_count + 1
                    
                    # Compute rolling average for centroid
                    new_lat = ((existing_family['lat'] * old_count) + lat) / new_count
                    new_lon = ((existing_family['lon'] * old_count) + lon) / new_count
                    
                    existing_family['lat'] = new_lat
                    existing_family['lon'] = new_lon
                    existing_family['count'] = new_count
                    
                    # Update the database record for the family
                    cursor.execute('''
                        UPDATE families 
                        SET latitude = ?, longitude = ?, student_count = ?
                        WHERE id = ?
                    ''', (new_lat, new_lon, new_count, assigned_family_id))
                    
                    break # Found the family, stop checking others with this last name
                    
        # 3. If no matching family was found (either new last name, or same last name but living far away)
        if assigned_family_id is None:
            # Create a brand new family
            cursor.execute('''
                INSERT INTO families (family_name, latitude, longitude, student_count, zone, phone_number, cycle_profile)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (last_name, lat, lon, 1, zone, '0555-000-000', 'MIXED'))

            assigned_family_id = cursor.lastrowid
            
            new_family_record = {
                'id': assigned_family_id,
                'lat': lat,
                'lon': lon,
                'count': 1,
                'zone': zone
            }
            
            if last_name not in families_dict:
                families_dict[last_name] = []
            families_dict[last_name].append(new_family_record)
            
        # 4. Prepare the student record for insertion
        student_insert_records.append((
            first_name,
            last_name,
            assigned_family_id,
            lat,
            lon,
            1  # is_active
        ))

    # Insert all students
    cursor.executemany('''
        INSERT INTO students (first_name, last_name, family_id, original_lat, original_lon, is_active, address, cycle)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
    ''', [(s[0], s[1], s[2], s[3], s[4], 'Ali Mendjeli, Constantine', 'Primary') for s in student_insert_records])

    conn.commit()
    
    # Get Final Stats
    cursor.execute('SELECT COUNT(*) FROM students')
    total_imported = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM families')
    total_families = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM families WHERE student_count > 1')
    families_with_siblings = cursor.fetchone()[0]
    
    cursor.execute('SELECT SUM(student_count) FROM families WHERE student_count > 1')
    total_siblings = cursor.fetchone()[0]
    
    print("\n✅ Data Import Complete!")
    print(f"Total students processed: {total_imported}")
    print(f"Total unique virtual stops (families) created: {total_families}")
    print(f"Families with >1 child (siblings detected): {families_with_siblings} (representing {total_siblings} students)")
    
    conn.close()

if __name__ == "__main__":
    import_students_and_detect_siblings()
