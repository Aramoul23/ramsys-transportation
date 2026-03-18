import pandas as pd
import random
import os

# Set seed for reproducibility
random.seed(42)

# Define exact regional bounds for Constantine
# Region 1: Ali Mendjeli (70%)
REGION_ALI_MENDJELI = {"min_lat": 36.2300, "max_lat": 36.2700, "min_lon": 6.5500, "max_lon": 6.6100}
# Region 2: Zouaghi & El Khroub (15%)
REGION_ZOUAGHI_KHROUB = {"min_lat": 36.2600, "max_lat": 36.2900, "min_lon": 6.6200, "max_lon": 6.7000}
# Region 3: Ain Smara & Center (15%)
REGION_CENTER_AIN_SMARA = {"min_lat": 36.2600, "max_lat": 36.3600, "min_lon": 6.5000, "max_lon": 6.6200} # Adjusted to encompass Ain smara & center

# The cycles we need to distribute students into
CYCLES = ['Kindergarten', 'Primary', 'Middle', 'High School']

# Target: 300 out of 700 are K/P = ~43%
# K: 18%, P: 25% (43%), M: 32%, H: 25% (57%)
CYCLE_WEIGHTS = [0.18, 0.25, 0.32, 0.25]

# Regional weights: 70%, 15%, 15%
REGIONS = [REGION_ALI_MENDJELI, REGION_ZOUAGHI_KHROUB, REGION_CENTER_AIN_SMARA]
REGION_WEIGHTS = [0.70, 0.15, 0.15]

def generate_coord(bounds):
    lat = round(random.uniform(bounds["min_lat"], bounds["max_lat"]), 6)
    lon = round(random.uniform(bounds["min_lon"], bounds["max_lon"]), 6)
    return lat, lon

def generate_family_data(target_total_students=700):
    last_names = [
        "BENALI", "BOUZID", "MANSOURI", "HADDAD", "SAIDI", "MEBARKI", "CHERIF",
        "AMRANI", "ZITOUNI", "BOUDIAF", "BELKACEM", "YAHIAOUI", "MOKHTARI",
        "GACEM", "TOUMI", "BOUCHAMA", "SLIMANI", "BOUALEM", "OTHMANI", "KADRI",
        "ZIANE", "LAHMADI", "MAHIOU", "KHELIFI", "DJOUDI", "RABAHI", "TALEB",
        "FERHAT", "BOUKHARI", "MERABET", "HAMZAOUI", "GUERROUDJ", "ZIDANE",
        "BOUTEFLIKA", "SELLAL", "OOUYAHIA", "TEBBOUNE", "CHENGRIHA", "NEZZAR",
        "TOUFANE", "BOUKEMACHE", "ABDELMOUMENE", "BENKHALFA", "BOUCHAREB",
        "DERRADJI", "GUELLIL", "HADDOUCHE", "KADDOUR", "LAMAMRA", "MEDELCI",
        "OUYAHIA", "REBRAB", "SAADANI", "MEZIANE", "KACEMI", "ZIANE", "BOUKAZOULA"
    ]
    
    first_names = [
        # Boys
        "Ahmed", "Mohamed", "Ali", "Omar", "Youssef", "Karim", "Aymen", "Ilyes",
        "Mehdi", "Walid", "Amine", "Nassim", "Riad", "Sofiane", "Tarek",
        "Anis", "Fares", "Hichem", "Kamel", "Mourad", "Nabil", "Rafik", "Said",
        "Tewfik", "Yacine", "Zine", "Abdelkader", "Boualem", "Chafik", "Djamel",
        # Girls
        "Fatima", "Amina", "Khadija", "Meriem", "Sarah", "Yasmine", "Imene",
        "Lina", "Manel", "Rania", "Amel", "Chahinez", "Dounia", "Feryel", "Hanane",
        "Ines", "Kenza", "Leila", "Malika", "Nadia", "Ouahiba", "Radia", "Samira",
        "Sonia", "Zohra", "Asma", "Baya", "Chaima", "Dalila"
    ]
    
    students = []
    
    # Generate exactly target_total_students
    while len(students) < target_total_students:
        family_name = random.choice(last_names)
        
        # Ensure we don't overshoot the 700 target
        remaining = target_total_students - len(students)
        if remaining >= 4:
            num_kids = random.choices([1, 2, 3, 4], weights=[0.4, 0.4, 0.15, 0.05])[0]
        elif remaining >= 3:
            num_kids = random.choices([1, 2, 3], weights=[0.45, 0.45, 0.10])[0]
        elif remaining >= 2:
            num_kids = random.choices([1, 2], weights=[0.5, 0.5])[0]
        else:
            num_kids = 1
            
        # Select region for this family based on 70/15/15 weights
        selected_region = random.choices(REGIONS, weights=REGION_WEIGHTS)[0]
        lat, lon = generate_coord(selected_region)
        
        # Make up address string based on selected region
        if selected_region == REGION_ALI_MENDJELI:
            address = "Nouvelle Ville Ali Mendjeli"
        elif selected_region == REGION_ZOUAGHI_KHROUB:
            address = random.choice(["Zouaghi Slimane", "El Khroub"])
        else:
            address = random.choice(["Ain Smara", "Centre Ville", "Sidi Mabrouk"])
            
        phone = f"0555-{random.randint(100,999):03d}-{random.randint(100,999):03d}"
        
        for k in range(num_kids):
            first_name = random.choice(first_names)
            cycle = random.choices(CYCLES, weights=CYCLE_WEIGHTS)[0]
            
            student = {
                "First Name": first_name,
                "Last Name": family_name,
                "Cycle": cycle,
                "Latitude": lat,
                "Longitude": lon,
                "Address": address,
                "Phone Number": phone
            }
            students.append(student)
            
    return students

if __name__ == "__main__":
    print("Generating ramsys_students_cycles_dummy.xlsx with 700 students (300 KP)...")
    
    data = generate_family_data(target_total_students=700)
    
    df = pd.DataFrame(data)
    
    # Show real distribution
    print("\nEducational Cycle Distribution:")
    print(df['Cycle'].value_counts())
    
    print("\nGeographic Regional Distribution:")
    print(df['Address'].value_counts(normalize=True).mul(100).round(1).astype(str) + '%')
    
    # Save to Excel
    filepath = "ramsys_students_cycles_dummy.xlsx"
    df.to_excel(filepath, index=False)
    
    print(f"\n✅ Successfully generated {len(data)} students across {df['Last Name'].nunique()} distinct families names (representing ~{int(len(data)/1.8)} virtual household stops).")
    print(f"✅ Saved to: {os.path.abspath(filepath)}")
