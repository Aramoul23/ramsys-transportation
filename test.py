"""
Test data generator — inserts fake families and students near downtown Constantine.
WARNING: This inserts data into the existing database. Use init_db.py first.
"""
import sqlite3
import random

conn = sqlite3.connect('ramsys_routing.db')
c = conn.cursor()

# Check if database has the required schema
try:
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='families'")
    if not c.fetchone():
        print("❌ Database not initialized. Run: python init_db.py")
        conn.close()
        exit(1)

    for i in range(1, 15):
        lat = 36.3650 + random.uniform(-0.02, 0.02)
        lon = 6.6140 + random.uniform(-0.02, 0.02)
        num_kids = random.randint(1, 3)

        # Insert family with all required fields
        c.execute(
            "INSERT INTO families (family_name, latitude, longitude, student_count, phone_number, cycle_profile) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"Family {i}", lat, lon, num_kids,
             f"0555-{random.randint(100,999):03d}-{random.randint(100,999):03d}",
             'MIXED')
        )
        fam_id = c.lastrowid

        # Insert students with all required fields
        cycles = ['Kindergarten', 'Primary', 'Middle', 'High School']
        for k in range(num_kids):
            c.execute(
                "INSERT INTO students (first_name, last_name, family_id, original_lat, original_lon, address, cycle) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"Kid {k+1}", f"Family {i}", fam_id, lat, lon,
                 'Centre Ville, Constantine', random.choice(cycles))
            )

    conn.commit()
    print("✅ 14 fake downtown Constantine families added with students!")
except Exception as e:
    print(f"❌ Error: {e}")
finally:
    conn.close()
