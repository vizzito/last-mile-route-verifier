# last-mile-route-verifier

Reproducibility tool for verifying route distances using a local [OSRM](http://project-osrm.org/) instance.

Used to validate and compare the results reported in:  
**"Last-Mile Route Optimization at Million-Stop Scale"** — [Medium article](https://medium.com/@martinvizzolini/last-mile-route-optimization-at-1-million-stops-with-near-linear-scaling-e4d4b0118e80)

---

## What it does

Given a JSON file containing Google Maps direction URLs (one list per route), this tool:

1. Starts a local OSRM routing engine via Docker
2. Queries OSRM for the real road distance of each segment
3. Reports total distance in km — **no Google Maps API key required**

This allows anyone to independently verify distance measurements using only open-source tools and OpenStreetMap data.

---

## Requirements

- Docker (for OSRM)
- Python 3.8+
- `pip install -r requirements.txt`

---

## Setup

### 1. Download OSM data for your region

Go to [Geofabrik](https://download.geofabrik.de/) and download the `.osm.pbf` file for the region you want to test.

```bash
mkdir -p data/massachusetts
cd data/massachusetts
wget https://download.geofabrik.de/north-america/us/massachusetts-latest.osm.pbf
```

### 2. Pre-process the OSM data with OSRM

```bash
# Extract
docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" \
  osrm/osrm-backend osrm-extract \
  -p /opt/car.lua /data/massachusetts-latest.osm.pbf

# Partition
docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" \
  osrm/osrm-backend osrm-partition /data/massachusetts-latest.osrm

# Customize
docker run --rm --platform linux/amd64 \
  -v "$(pwd)/data/massachusetts:/data" \
  osrm/osrm-backend osrm-customize /data/massachusetts-latest.osrm
```

### 3. Start OSRM

```bash
./run_osrm.sh massachusetts 5002
```

OSRM will be available at `http://localhost:5002`.

---

## Usage

```bash
python scripts/check_distance_osrm.py \
  --input samples/example_routes.json \
  --osrm http://localhost:5002 \
  --workers 6
```

### Input format

A JSON file with a list of routes. Each route is a list of Google Maps direction URLs:

```json
[
  [
    "https://www.google.com/maps/dir/?api=1&origin=LAT,LNG&destination=LAT,LNG&waypoints=LAT,LNG|LAT,LNG&travelmode=driving"
  ],
  [
    "..."
  ]
]
```

---

## Reproducing paper results

The Amazon Last Mile dataset routes and measured distances reported in the paper can be verified by:

1. Downloading the [Amazon Last Mile Routing Research Challenge dataset](https://registry.opendata.aws/amazon-last-mile-routing-research/)
2. Converting the historical routes to the Google Maps URL format (see `scripts/`)
3. Running this tool against your local OSRM instance for the corresponding region

---

## License

MIT
