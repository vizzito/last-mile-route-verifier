# last-mile-route-verifier

An open-source tool for independently verifying and comparing last-mile delivery route distances using a **local OSRM instance** — no proprietary API keys or cloud services required.

Built to reproduce and validate the results reported in:
**"Last-Mile Route Optimization at Million-Stop Scale"** — [Medium article](https://medium.com/@martinvizzolini/last-mile-route-optimization-at-1-million-stops-with-near-linear-scaling-e4d4b0118e80)

---

## What problem does this solve?

When an optimization solver claims to reduce total delivery distance by X%, how do you independently verify that claim?

This tool answers that question. Given a JSON file of delivery routes encoded as Google Maps direction URLs, it computes the **actual road distance** of every route by querying a local routing engine (OSRM). You can run it on two datasets covering the same stops — the **Amazon historical routes** and the **solver-generated routes** — and compare the total kilometers to verify the improvement.

No third-party APIs are involved. The only external data source is OpenStreetMap, downloaded once and processed locally.

---

## Screenshots

### Interactive route map with summary panel

Each verification run automatically generates an HTML map. The panel shows total routes, stops, km, and per-route averages. Each route has a distinct color.

![Route map with summary panel](images/Screenshot%202026-04-07%20at%2000.24.02.png)

### Stop sequence overlay (eye icon per route)

Click the eye icon next to any route in the panel to overlay delivery sequence numbers directly on the map. The view auto-zooms to fit the selected route. Click again to hide.

![Stop sequence numbers on map](images/Screenshot%202026-04-07%20at%2000.24.46.png)

---

## Quick Start

> Prerequisites: Docker installed, Python 3.8+ with `pip install -r requirements.txt`

> **Important:** a running OSRM server is required before executing the verifier. Without it, distance queries cannot be made.

> The sample datasets included (`DBO1`, `DBO2`, `DBO3`) cover the **Boston area** and are the recommended starting point. Downloading the **Massachusetts** map is sufficient to run all three. Other depots (`DSE4`, `DLA4`, `DAU1`, `DCH2`) require their own regional map — see the depot table in [Included sample datasets](#included-sample-datasets).

```bash
# 1. Download and pre-process the Massachusetts map (one-time setup)
mkdir -p data/massachusetts
wget https://download.geofabrik.de/north-america/us/massachusetts-latest.osm.pbf \
     -O data/massachusetts/massachusetts-latest.osm.pbf

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/massachusetts-latest.osm.pbf

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-partition /data/massachusetts-latest.osrm

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-customize /data/massachusetts-latest.osrm

# 2. Configure your environment
cp .env.example .env
# Set OSRM_REGION=massachusetts in .env

# 3. Start the OSRM server
docker compose up osrm -d

# 4. Verify the Amazon historical routes for depot DBO1
#    (60 routes, Boston area — reconstructed from GPS tracking data)
python3 scripts/check_distance_osrm.py \
  --input inputs/amazon/routes_result_DBO1_AMZ_60.json \
  --osrm  http://localhost:5002 \
  --workers 6
# → prints TOTAL DISTANCE and saves map to maps/AMZ_DBO1.html

# 5. Verify the solver-generated routes for the same depot
#    (same delivery stops, optimized by the solver)
python3 scripts/check_distance_osrm.py \
  --input inputs/solver/routes_result_DBO1_56.json \
  --osrm  http://localhost:5002 \
  --workers 6
# → prints TOTAL DISTANCE and saves map to maps/SOLVER_DBO1.html

# Compare the two TOTAL DISTANCE values — the difference is the solver's improvement
```

---

## Included sample datasets

The `inputs/` folder contains pre-computed route results for several depots from the Amazon Last Mile dataset. **This tool reads those results — it does not run the optimization solver or generate routes.** The solver has already been executed; the JSON files are its output.

### What is a depot?

Each JSON file corresponds to one **depot** — a distribution center that serves a specific delivery area. All routes in a file start and end at the same warehouse.

| Depot code | Area | OSRM region needed |
|---|---|---|
| `DBO1`, `DBO2`, `DBO3` | Boston, MA | Massachusetts |
| `DSE4` | Seattle, WA | Washington |
| `DLA4` | Los Angeles, CA | California |
| `DAU1` | Austin, TX | Texas |
| `DCH2` | Chicago, IL | Illinois |

> The sample datasets cover different US cities. Download the OSRM map for the region you want to run. **Massachusetts** covers all the `DBO` depots and is the recommended starting point.

### What is inside each file?

Each file contains the **delivery route plan** for that depot — expressed as a list of routes, where each route is a list of Google Maps direction URLs. Each URL encodes up to 50 ordered GPS stop coordinates as `origin`, `destination`, and `waypoints` query parameters.

This encoding is used as a **portable coordinate container**, not as a Google Maps API call. The tool parses only the coordinate fields from the URL — nothing is sent to Google. You can paste any URL into a browser to inspect the stops visually.

```json
{
  "delivery_stop_count": 8205,
  "routes": [
    [
      "https://www.google.com/maps/dir/?api=1&origin=42.3601,-71.0589&destination=42.3651,-71.0612&waypoints=42.3621,-71.0598|42.3635,-71.0605|...&travelmode=driving",
      "https://www.google.com/maps/dir/?api=1&origin=42.3651,-71.0612&destination=42.3701,-71.0630&waypoints=..."
    ],
    [
      "https://www.google.com/maps/dir/?api=1&origin=42.3801,-71.0700&destination=42.3850,-71.0720&waypoints=..."
    ]
  ]
}
```

| Level | Represents |
|---|---|
| `delivery_stop_count` | Total delivery stops for this depot (from solver metadata) |
| Outer array (`routes`) | One entry per route (one vehicle's full day) |
| Inner array | One URL per segment — each covers up to 50 stops |
| URL parameters | Ordered GPS coordinates: `origin`, `waypoints`, `destination` |

### Why up to 50 stops per URL?

Google Maps web supports 10 stops; the Google Directions API supports 25. Neither limit applies to OSRM, which handles up to 50 in a single HTTP request. Packing 50 stops per URL minimizes query count and speeds up processing.

---

## Amazon routes vs solver routes

Both `inputs/amazon/` and `inputs/solver/` contain results for the **same depots and the same delivery stops**. The difference is who planned the routes:

| Folder | What it contains |
|---|---|
| `inputs/amazon/` | Historical routes actually driven by Amazon delivery drivers, reconstructed from GPS tracking data. These represent how deliveries were executed in practice. |
| `inputs/solver/` | Routes generated by the optimization solver for the same set of stops, re-sequenced and redistributed across vehicles to minimize total driving distance. |

For each depot there is a matching pair of files — one in each folder. Running the verifier on both and comparing `TOTAL DISTANCE` gives an independent, reproducible measurement of the distance reduction achieved by the solver.

**Solver constraints:** the solver routes were built under real operational constraints — maximum stops per route, vehicle weight capacity, cargo volume capacity, and delivery time windows. These constraints are part of the solver input; only the resulting GPS stop coordinates are stored in the JSON files. Delivery volumes and weights are not visualized in the maps.

---

## What is OSRM?

**OSRM (Open Source Routing Machine)** is a high-performance routing engine built on **OpenStreetMap (OSM)** road data. It computes shortest-path driving distances using the Multi-Level Dijkstra (MLD) algorithm on pre-processed road graphs.

Key properties:

- **Offline and free:** runs entirely on your machine using OSM data from [Geofabrik](https://download.geofabrik.de/)
- **Deterministic:** same map and same coordinates always return the same distance
- **Road-aware:** accounts for drivable roads, turn restrictions, and one-way streets
- **Fast:** graph pre-processing enables sub-millisecond query times

OSRM is queried via its HTTP API:

```
GET /route/v1/driving/{lon1},{lat1};{lon2},{lat2};...?overview=false
```

The response includes a `distance` field in meters, which this tool converts to kilometers.

---

## Output

```
================================================================
  LAST-MILE ROUTE DISTANCE VERIFIER
================================================================
  Input    : inputs/amazon/routes_result_DBO1_AMZ_60.json
  OSRM     : http://localhost:5002
  Routes   : 60   |   Segments: 200   |   Workers: 6
  Stop mode: chain  (waypoints-only 8069 | chain-merged 8205 delivery coords)
================================================================

Processing 200 segment(s) using 6 parallel worker(s)...

  [  OK]    1/200  Route   1  Seg 1     2.341 km  (50 stops)
  [  OK]    2/200  Route   1  Seg 2     3.812 km  (50 stops)
  ...

================================================================
  ROUTE SUMMARY
----------------------------------------------------------------
  Route   1  |  4 segments   |    31.204 km
  Route   2  |  3 segments   |    18.917 km
  ...
----------------------------------------------------------------
  Total routes    : 60
  Total segments  : 200
  Total points    : 8,205

  TOTAL DISTANCE  :   1,234.560 km
================================================================

Generating map → maps/AMZ_DBO1.html ...
Map saved      → maps/AMZ_DBO1.html
```

| Field | Description |
|---|---|
| `stops` | Coordinate count in that segment |
| `km` per segment | Road distance returned by OSRM |
| `km` per route | Sum of all segment distances for that route |
| `Total points` | Canonical stop count from the JSON metadata |
| `TOTAL DISTANCE` | Sum across all routes — the number to compare between datasets |

---

## Requirements

- **Docker** (for OSRM)
- **Python 3.8+** with `pip install -r requirements.txt`
- OSM map data for your region (downloaded once, stored in `data/`)

> **Important:** the OSRM server must be running before executing any verification. Start it with `docker compose up osrm -d` and wait a few seconds for the road graph to load.

---

## Setup — map data pre-processing

This step is required once per region. The output files are stored in `data/<region>/` and reused on every subsequent run.

Download the `.osm.pbf` file for your region from [Geofabrik](https://download.geofabrik.de/), then run the three pre-processing steps:

```bash
# Example for Massachusetts (covers DBO1, DBO2, DBO3 sample datasets)
mkdir -p data/massachusetts
wget https://download.geofabrik.de/north-america/us/massachusetts-latest.osm.pbf \
     -O data/massachusetts/massachusetts-latest.osm.pbf

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/massachusetts-latest.osm.pbf

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-partition /data/massachusetts-latest.osrm

docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" osrm/osrm-backend \
  osrm-customize /data/massachusetts-latest.osrm
```

> Pre-processing a US state takes a few minutes and requires approximately 1.5 GB of disk space.

---

## Region validation — wrong map detection

Before processing any routes, the verifier automatically checks that the OSRM server covers the same geographic area as your input data.

A few coordinates from your input are sent to OSRM's `/nearest` endpoint. If the correct map is loaded, snap distances are typically under 200 meters. If the map is wrong, snapping lands thousands of kilometers away.

**When the map matches:**
```
  [  OK]  (42.36123, -71.05891)  →  nearest road:       38.2 m
  Detected region  : Massachusetts / New England
  Map region matches input data. Proceeding.
```

**When the map is wrong:**
```
  [FAIL]  (30.44524, -97.70942)  →  nearest road: 1,066,094.8 m
  ERROR: The loaded OSRM map does not cover this dataset's region.

  Detected region  : Texas
  Geofabrik slug   : north-america/us/texas
  Download and pre-process the Texas map, then restart OSRM with OSRM_REGION=texas.
```

The script exits immediately — no queries are wasted, and the fix is shown inline.

---

## Running with Docker

```bash
# Configure environment
cp .env.example .env
# Set OSRM_REGION=massachusetts (or your region) in .env

# Start OSRM
docker compose up osrm -d

# Run the verifier (map is saved automatically based on the input filename)
docker compose run --rm verifier \
  --input inputs/amazon/routes_result_DBO1_AMZ_60.json \
  --osrm  http://osrm:5000

# Stop OSRM when done
docker compose down
```

> Inside Docker Compose, use `http://osrm:5000` as the OSRM URL. When running natively, use `http://localhost:5002`.

---

## Running natively

```bash
pip install -r requirements.txt

# Start OSRM
./run_osrm.sh massachusetts 5002

# Verify Amazon routes (map auto-saved to maps/AMZ_DBO1.html)
python3 scripts/check_distance_osrm.py \
  --input   inputs/amazon/routes_result_DBO1_AMZ_60.json \
  --osrm    http://localhost:5002 \
  --workers 6

# Verify solver routes (map auto-saved to maps/SOLVER_DBO1.html)
python3 scripts/check_distance_osrm.py \
  --input   inputs/solver/routes_result_DBO1_56.json \
  --osrm    http://localhost:5002 \
  --workers 6
```

### CLI options

| Option | Default | Description |
|---|---|---|
| `--input` | required | Path to the JSON input file |
| `--osrm` | `http://localhost:5002` | Base URL of the OSRM server |
| `--workers` | `6` | Parallel threads for OSRM queries |
| `--map` | auto from filename | Output path for the HTML map (e.g. `maps/amazon.html`) |
| `--no-map` | off | Skip HTML map generation |
| `--label` | auto from filename | Title shown on the map panel |

The map output path and label are automatically inferred from the input filename when they follow the `routes_result_<CODE>_AMZ_<n>.json` or `routes_result_<CODE>_<n>.json` pattern. Pass `--map` and `--label` explicitly for files with generic names.

---

## HTML maps (visualizing routes)

`generate_map.py` produces an interactive HTML map from any route JSON file — no OSRM required. Coordinates are read directly from the URLs; no external API is called.

Each map includes:

- Colored circle markers for every delivery stop (one color per route)
- Square depot markers (black outline, light gray fill) at each route's starting warehouse
- Collapsible route summary panel (top-right): total routes, stops, km, averages, and per-route breakdown
- Optional top-center banner with a description of the dataset

```bash
# Generate a map for any route JSON (no OSRM needed)
python3 scripts/generate_map.py \
  --input   inputs/amazon/routes_result_DBO1_AMZ_60.json \
  --output  maps/DBO1_amazon.html \
  --label-a "DBO1 Amazon"
```

**Map interactive features:**

- **Route summary panel** — collapsible; shows total stops, total km, stops/route, km/route, and per-route detail
- **Eye icon per route** — click to overlay delivery sequence numbers (1, 2, 3…) on the map for that route; the view auto-zooms to fit it. Click again to hide
- **Top-center banner** — dataset title with an optional description. Click the × to dismiss it

### `generate_map.py` options

| Option | Default | Description |
|---|---|---|
| `--input` | required | Route JSON file |
| `--output` | `map.html` | Output HTML file path |
| `--label-a` | `Amazon` | Dataset label shown in the panel and banner |
| `--description` | none | Subtitle shown in the top-center banner |
| `--delivery-stops` | none | Override the displayed stop count |

---

## Alternative: OpenRouteService (online, no Docker required)

[OpenRouteService (ORS)](https://openrouteservice.org/) is a free online routing API powered by OpenStreetMap. It accepts the same input format and requires no local infrastructure — just a free API key.

**When to use ORS instead of OSRM:**

- No Docker or local map downloads desired
- Quick cross-check against a different routing engine
- Running on a machine without Docker

**Limitation:** ORS is rate-limited. The free tier allows 2,000 requests/day and 40 requests/minute. For large datasets, OSRM is significantly faster.

### Setup

Create a free account at [openrouteservice.org/dev/#/signup](https://openrouteservice.org/dev/#/signup) and generate an API key from the dashboard under **Tokens**.

```bash
export ORS_API_KEY=your_key_here
python3 scripts/check_distance_ors.py --input inputs/amazon/routes_result_DBO1_AMZ_60.json
```

### ORS CLI options

| Option | Default | Description |
|---|---|---|
| `--input` | required | Path to the JSON input file |
| `--wait` | `1.6` | Seconds between requests. Keep at 1.5 or above on the free tier |
| `--retries` | `3` | Max retry attempts per segment on rate limit or failure |

### Free tier limits

| Limit | Value |
|---|---|
| Requests per day | 2,000 |
| Requests per minute | 40 |
| Max stops per request | 50 |

The script uses 1.6 seconds between requests by default (~37 req/min). HTTP 429 triggers automatic exponential backoff and retry.

> Differences of 1–3% between ORS and OSRM results are normal (different routing engines, different map data versions).

---

## Project structure

```
last-mile-route-verifier/
├── inputs/
│   ├── amazon/          # Pre-computed historical Amazon driver routes (Public dataset, one file per depot)
│   └── solver/          # Pre-computed optimizer-generated routes (same stops, one file per depot)
├── data/
│   └── massachusetts/   # Pre-processed OSRM map files (not in git, generated locally)
├── maps/                # Generated HTML maps (not in git)
├── images/              # Screenshots used in this README
├── scripts/
│   ├── check_distance_osrm.py  # Main verifier (local OSRM, parallel)
│   ├── check_distance_ors.py   # Alternative verifier (ORS online API, sequential)
│   ├── count_json_coords.py    # Count URL coordinate tokens
│   ├── generate_map.py         # Interactive HTML map from route JSON
│   └── validate_route_delivery_count.py  # Validate stop counts vs metadata
├── Dockerfile
├── docker-compose.yml
├── run_osrm.sh
├── requirements.txt
└── .env.example
```

---

## License

MIT
