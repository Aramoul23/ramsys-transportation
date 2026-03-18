import pandas as pd
import numpy as np
import random
import os

# Set seed for reproducibility
np.random.seed(42)
random.seed(42)

# Define the zones and their approximate center coordinates in Constantine
ZONES = {
    'Nouvelle Ville Ali Mendjeli': {'lat': 36.2480, 'lon': 6.5700, 'weight': 0.45},
    'Zouaghi Slimane': {'lat': 36.2890, 'lon': 6.6230, 'weight': 0.25},
    'El Khroub': {'lat': 36.2658, 'lon': 6.6975, 'weight': 0.20},
    'Centre Ville': {'lat': 36.3650, 'lon': 6.6147, 'weight': 0.04},
    'Daksi': {'lat': 36.3500, 'lon': 6.6350, 'weight': 0.03},
    'Sidi Mabrouk': {'lat': 36.3550, 'lon': 6.6200, 'weight': 0.03}
}

# Common Algerian Last Names
LAST_NAMES = ['Benali', 'Bouzid', 'Saidi', 'Merzak', 'Hamidi', 'Belkacem', 'Brahimi', 'Taleb', 'Othmani', 'Mansouri', 'Kamel', 'Nasri', 'Cherif', 'Haddad', 'Bouchama', 'Zerrouki', 'Amrani', 'Boudiaf', 'Toumi', 'Ziane', 'Chabane', 'Mebarki', 'Yahiaoui', 'Mokhtari', 'Boualem', 'Gacem', 'Latreche']

# Common Algerian First Names
FIRST_NAMES_M = ['Ali', 'Mohamed', 'Karim', 'Youssef', 'Omar', 'Amine', 'Walid', 'Tarik', 'Nadir', 'Riad', 'Adel', 'Fares', 'Sofiane', 'Mehdi', 'Aymen']
FIRST_NAMES_F = ['Amina', 'Fatima', 'Sarah', 'Meriem', 'Lina', 'Ines', 'Yasmine', 'Rania', 'Khadija', 'Nour', 'Asma', 'Chaima', 'Manel', 'Wissam', 'Imane']

def generate_random_coords(center_lat, center_lon, radius_km=1.5):
    # 1 degree of latitude is roughly 111 km
    radius_in_degrees = radius_km / 111.0
    u = random.uniform(0, 1)
    v = random.uniform(0, 1)
    w = radius_in_degrees * np.sqrt(u)
    t = 2 * np.pi * v
    x = w * np.cos(t)
    y = w * np.sin(t) / np.cos(np.radians(center_lat))
    return round(center_lat + x, 6), round(center_lon + y, 6)

def generate_dummy_data(num_students=500):
    print(f"Generating dummy data for {num_students} students...")
    students = []
    
    # Pre-select family structures to ensure we have siblings
    # 50% single child, 35% two kids, 10% three kids, 5% four kids
    num_families = int(num_students / 1.7) # rough estimate of total families
    
    current_student_count = 0
    
    while current_student_count < num_students:
        # Pick a random zone based on weights
        zone = random.choices(list(ZONES.keys()), weights=[v['weight'] for v in ZONES.values()])[0]
        
        # Determine family size (1 to 4)
        family_size = random.choices([1, 2, 3, 4], weights=[0.50, 0.35, 0.10, 0.05])[0]
        
        # Don't exceed the requested number of students
        if current_student_count + family_size > num_students:
            family_size = num_students - current_student_count
            
        last_name = random.choice(LAST_NAMES)
        
        # Generate the family's exact home location
        lat, lon = generate_random_coords(ZONES[zone]['lat'], ZONES[zone]['lon'], radius_km=1.5 if zone == 'Nouvelle Ville Ali Mendjeli' else 1.0)
        
        # Sometimes siblings' GPS might jitter a TINY bit due to different phone readings, 
        # but they will be very close (e.g. 10 meters apart)
        
        for i in range(family_size):
            gender_is_male = random.choice([True, False])
            first_name = random.choice(FIRST_NAMES_M if gender_is_male else FIRST_NAMES_F)
            
            # Add tiny jitter (max ~15 meters) to simulate real-world messy GPS data
            jitter_lat = lat + random.uniform(-0.0001, 0.0001)
            jitter_lon = lon + random.uniform(-0.0001, 0.0001)
            
            students.append({
                'last_name': last_name,
                'first_name': first_name,
                'latitude': round(jitter_lat, 6),
                'longitude': round(jitter_lon, 6),
                'zone_label': zone
            })
            
            current_student_count += 1

    df = pd.DataFrame(students)
    
    # Shuffle the dataframe fully to simulate a messy Excel file where siblings aren't always next to each other
    df = df.sample(frac=1).reset_index(drop=True)
    
    output_file = 'sample_students.xlsx'
    df.to_excel(output_file, index=False)
    print(f"✅ Created {output_file} with {len(df)} students.")
    print("\nApproximate distribution:")
    print(df['zone_label'].value_counts())
    
if __name__ == "__main__":
    generate_dummy_data()
