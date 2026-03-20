# Ramsys School Bus Route Optimizer — Improved v3.5

A high-performance school bus routing system for Ramsys School, Constantine, Algeria. This version integrates **Google OR-Tools** for optimization, **HERE Maps API** for real-time traffic-aware matrices, and a **local OSRM engine** for offline turn-by-turn navigation.

## 🚀 Key Improvements
- **Zero-Cost Stack**: Uses SQLite and local OSRM (Docker) to minimize operational costs.
- **Traffic Integration**: Connects to HERE Maps Routing API v8 for historical/live traffic matrices.
- **Fleet-Dynamic VRP**: Automatically adjusts to the number of active buses in the database.
- **Sibling Constraint**: Ensures siblings always ride the same bus using GPS proximity

 detection (50m).
- **Driver Integration**: Generates optimized stop sequences for the [Ramsys Smart Bus](https://github.com/Aramoul23/ramsys-smart-bus) driver application.

---

## 🛠️ Setup Instructions

### 1. Environment Configuration
Rename `.env.example` to `.env` and fill in the following:
- `HERE_API_KEY`: Your free key from [developer.here.com](https://developer.here.com/) (Required for traffic optimization).
- `FLASK_SECRET_KEY`: A strong random string for session security.

### 2. Dependency Installation
```bash
pip install -r requirements.txt
```

### 3. Database Initialization
```bash
python init_db.py --force
```

### 4. Running the App
```bash
python app.py
```
- Admin Dashboard: `http://localhost:5000/admin`
- Credentials: `admin` / `ramsys2026`

---

## 🗺️ OSRM Mapping Data (Algeria)

The large mapping files required for turn-by-turn navigation are excluded from this repository (approx. 1.5GB total).

### How to set up OSRM locally:
1. **Install Docker Desktop**: Required to run the OSRM server.
2. **Download Map Extract**:
   ```bash
   wget https://download.geofabrik.de/africa/algeria-latest.osm.pbf
   ```
3. **Preprocess and Run**:
   Use the `bus.lua` profile provided in this repo to ensure routes are optimized for bus physical constraints and speeds.

For a full setup guide, refer to [Phase 0 documentation](ramsys_bus_routing_system_prompt_v2.md#phase-0-infrastructure-setup).

---

## 📊 Workflow
1. **Import Students**: Upload your student Excel list to the Admin Dashboard (supports GPS proximity sibling detection).
2. **Sync Traffic**: Run the HERE Matrix Fetcher to get fresh travel times.
3. **Optimize**: Run the OR-Tools solver to generate ordered stops.
4. **Deploy**: The system automatically updates the database for the Driver PWA.

---
© 2026 RAMSYS Transportation — Groupement Scolaire Ramsys, Constantine.


Thank you 