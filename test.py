import sqlite3
import random

conn = sqlite3.connect('ramsys_routing.db')
c = conn.cursor()
for i in range(1, 15):
    # Insert a fake family in downtown Constantine coordinates
    c.execute("INSERT INTO families (family_name, latitude, longitude) VALUES (?, ?, ?)", 
              (f"Family {i}", 36.3650 + random.uniform(-0.02, 0.02), 6.6140 + random.uniform(-0.02, 0.02)))
    fam_id = c.lastrowid
    
    # Give this family 1 to 3 kids
    num_kids = random.randint(1, 3)
    for k in range(num_kids):
        c.execute("INSERT INTO students (first_name, family_id) VALUES (?, ?)", (f"Kid {k+1}", fam_id))
        
conn.commit()
conn.close()
print("15 fake downtown families added!")