"""
check_distance_osrm.py
======================
Verifies total route distances using a local OSRM (Open Source Routing Machine) instance.

Given a JSON file with a list of Google Maps-format direction URLs (one list per route),
this script queries OSRM for the actual road distance of each segment and reports
per-route and total distances in kilometers.

Typical use case:
    Compare the total distance driven by Amazon drivers (inputs/amazon/routes.json)
    against routes produced by an optimization solver (inputs/solver/routes.json)
    to validate the solver's reported improvement.

Usage:
    python scripts/check_distance_osrm.py \\
        --input inputs/amazon/routes_result_DLA4_AMZ_197.json \\
        --osrm  http://localhost:5002 \\
        --workers 6
    # By default writes maps/AMZ_DLA4.html with title "DLA4 AMAZON (OSRM)".
    # Use --no-map to skip HTML, or pass --map / --label to override.

Requirements:
    pip install -r requirements.txt
    OSRM server running locally (see docker-compose.yml or run_osrm.sh)
"""

import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from generate_map import (
    CoordMode,
    build_map_from_route_data,
    delivery_coord_totals,
    load_routes_json_payload,
    resolve_delivery_coord_mode,
    route_to_coords,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EARTH_RADIUS_M: float = 6_371_000    # Mean Earth radius in meters
OSRM_ROAD_FACTOR: float = 1.5        # Straight-line to road-distance multiplier (haversine fallback)
MIN_DISTANCE_KM: float = 0.01        # Minimum plausible segment distance in km
OSRM_REQUEST_TIMEOUT: int = 10       # HTTP request timeout in seconds

# Region validation: maximum tolerated snap distance from a sample coordinate to the
# nearest road on the loaded OSRM map.  If the OSRM server is serving the wrong region
# (e.g., Massachusetts map for California routes) the nearest road will typically be
# thousands of km away.  5 km is a very generous threshold — in practice, delivery stops
# are almost always within 200 m of a drivable road.
REGION_VALIDATION_SNAP_THRESHOLD_M: float = 5_000   # meters
REGION_VALIDATION_SAMPLE_SIZE: int = 5              # coordinates to sample from input

# Known regions: (min_lat, max_lat, min_lon, max_lon) → (display_name, geofabrik_slug)
# Used to suggest the correct OSRM map and download URL when validation fails.
# Geofabrik slugs follow the pattern: https://download.geofabrik.de/<slug>-latest.osm.pbf
KNOWN_REGIONS: List[Tuple[Tuple[float, float, float, float], str, str]] = [
    ((42.0, 47.5, -74.0, -66.9),   "Massachusetts / New England",  "north-america/us/massachusetts"),
    ((32.5, 42.1, -124.5, -114.1), "California",                   "north-america/us/california"),
    ((40.4, 45.1, -79.8, -71.8),   "New York",                     "north-america/us/new-york"),
    ((41.4, 42.5, -88.3, -87.4),   "Illinois / Chicago",           "north-america/us/illinois"),
    ((25.8, 36.5, -106.7, -93.5),  "Texas",                        "north-america/us/texas"),
    ((25.0, 31.0, -87.7, -80.0),   "Florida",                      "north-america/us/florida"),
    ((38.8, 39.8, -77.6, -76.5),   "Washington DC / Maryland",     "north-america/us/maryland"),
    ((32.5, 35.3, -85.0, -78.5),   "Georgia / Atlanta",            "north-america/us/georgia"),
    ((47.0, 49.0, -122.6, -116.9), "Washington State / Seattle",   "north-america/us/washington"),
    ((-35.0, -34.0, -59.0, -57.5), "Buenos Aires",                 "south-america/argentina"),
    ((-24.0, -22.0, -47.0, -42.8), "São Paulo",                    "south-america/brazil/sudeste"),
    ((51.2, 51.7,  -0.5,   0.3),   "London",                       "europe/great-britain/england/greater-london"),
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RouteData:
    """Aggregated result for a single route, used for map generation."""
    route_index: int
    coords: List[Tuple[float, float]]   # Delivery stops in order (depot excluded)
    distance_km: float
    segment_count: int
    stop_count: int
    depot: Optional[Tuple[float, float]] = None  # First-segment origin (warehouse)


@dataclass
class SegmentResult:
    """Holds the distance result for a single URL segment queried from OSRM."""
    route_index: int
    segment_index: int
    distance_km: float
    waypoint_count: int
    url: str
    skipped: bool = False
    used_haversine_fallback: bool = False


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_distance(
    point_a: Tuple[float, float],
    point_b: Tuple[float, float],
) -> float:
    """
    Returns the great-circle (straight-line) distance between two coordinates in meters.

    Used only as a last-resort fallback when OSRM returns a zero-distance result.
    The result is multiplied by OSRM_ROAD_FACTOR (~1.5x) to approximate actual road distance.

    Args:
        point_a: (latitude, longitude) in decimal degrees.
        point_b: (latitude, longitude) in decimal degrees.

    Returns:
        Straight-line distance in meters.
    """
    lat1, lon1 = math.radians(point_a[0]), math.radians(point_a[1])
    lat2, lon2 = math.radians(point_b[0]), math.radians(point_b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.asin(math.sqrt(a))


def haversine_polyline_approx_km(points: List[Tuple[float, float]]) -> float:
    """
    Approximate path length (km) along ``points`` as sum of great-circle legs,
    scaled by OSRM_ROAD_FACTOR — used when OSRM cannot return a route.
    """
    if len(points) < 2:
        return 0.0
    total_m = 0.0
    for i in range(len(points) - 1):
        total_m += haversine_distance(points[i], points[i + 1])
    return OSRM_ROAD_FACTOR * total_m / 1000.0


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def extract_coordinates_from_url(
    url: str,
) -> Tuple[
    Optional[Tuple[float, float]],
    Optional[Tuple[float, float]],
    List[Tuple[float, float]],
]:
    """
    Extracts origin, destination, and intermediate waypoints from a direction URL.

    The URL format uses standard query parameters:
        origin      = LAT,LNG
        destination = LAT,LNG
        waypoints   = LAT,LNG|LAT,LNG|...

    Note: Although the URL domain is google.com, this tool does NOT call Google's services.
    The URL is used purely as a portable container for coordinate data — the same
    coordinate string format is supported by any compatible routing frontend.
    Google Maps web displays up to 10 stops; the Google Directions API supports up to 25.
    URLs in this project encode up to 50 stops, which is the maximum supported by OSRM,
    allowing full utilization of the routing engine's capacity.

    Waypoints may include an optimization flag prefix (e.g., "optimize:true|" or
    "optimize:false|") which is stripped before coordinate parsing.

    Args:
        url: A direction URL string with coordinate parameters.

    Returns:
        A tuple of (origin, destination, waypoints).
        Each element is a (lat, lng) float tuple, or None/empty list if absent or invalid.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    def parse_coord(s: Optional[str]) -> Optional[Tuple[float, float]]:
        if not s:
            return None
        try:
            lat, lng = map(float, s.split(","))
            return (lat, lng)
        except (ValueError, AttributeError):
            return None

    origin = parse_coord(qs.get("origin", [None])[-1])
    destination = parse_coord(qs.get("destination", [None])[-1])

    waypoints: List[Tuple[float, float]] = []
    waypoints_str = qs.get("waypoints", [None])[-1]
    if waypoints_str:
        # Strip routing optimization flag if present (e.g., "optimize:true|")
        waypoints_str = re.sub(r"optimize:(true|false)\|", "", waypoints_str, count=1)
        for wp in waypoints_str.split("|"):
            coord = parse_coord(wp.strip())
            if coord:
                waypoints.append(coord)

    return origin, destination, waypoints


# ---------------------------------------------------------------------------
# Region validation
# ---------------------------------------------------------------------------

def _suggest_region(
    bbox: Tuple[float, float, float, float],
    osrm_base_url: str,
) -> None:
    """
    Prints a suggested OSRM region and ready-to-run commands based on the input bounding box.

    Scans KNOWN_REGIONS for the entry whose bounding box best overlaps the input data.
    If a match is found, prints the Geofabrik download URL and the exact Docker /
    docker compose commands to start the correct OSRM server.

    Args:
        bbox:          (min_lat, max_lat, min_lon, max_lon) of the input dataset.
        osrm_base_url: The OSRM URL currently in use (used to extract the port).
    """
    min_lat, max_lat, min_lon, max_lon = bbox

    best_match: Optional[Tuple[str, str]] = None
    best_overlap: float = 0.0

    for (r_min_lat, r_max_lat, r_min_lon, r_max_lon), name, slug in KNOWN_REGIONS:
        # Compute intersection area as a simple overlap score
        lat_overlap = max(0.0, min(max_lat, r_max_lat) - max(min_lat, r_min_lat))
        lon_overlap = max(0.0, min(max_lon, r_max_lon) - max(min_lon, r_min_lon))
        overlap = lat_overlap * lon_overlap
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = (name, slug)

    # Extract port from the OSRM URL for the suggested command
    try:
        port = urlparse(osrm_base_url).port or 5002
    except Exception:
        port = 5002

    geofabrik_base = "https://download.geofabrik.de"

    print()
    print("  ── Suggested fix ──────────────────────────────────────────")
    if best_match and best_overlap > 0:
        name, slug = best_match
        region_folder = slug.split("/")[-1]
        osm_file = f"{region_folder}-latest.osm.pbf"
        osrm_file = f"{region_folder}-latest.osrm"
        print(f"  Detected region  : {name}")
        print(f"  Geofabrik slug   : {slug}")
        print()
        print("  1. Download the correct map (one-time):")
        print(f"       mkdir -p data/{region_folder}")
        print(f"       wget {geofabrik_base}/{slug}-latest.osm.pbf \\")
        print(f"            -O data/{region_folder}/{osm_file}")
        print()
        print("  2. Pre-process (one-time per map file):")
        print(f"       docker run --rm --platform linux/amd64 \\")
        print(f"         -v \"$(pwd)/data/{region_folder}:/data\" osrm/osrm-backend \\")
        print(f"         osrm-extract -p /opt/car.lua /data/{osm_file}")
        print(f"       docker run --rm --platform linux/amd64 \\")
        print(f"         -v \"$(pwd)/data/{region_folder}:/data\" osrm/osrm-backend \\")
        print(f"         osrm-partition /data/{osrm_file}")
        print(f"       docker run --rm --platform linux/amd64 \\")
        print(f"         -v \"$(pwd)/data/{region_folder}:/data\" osrm/osrm-backend \\")
        print(f"         osrm-customize /data/{osrm_file}")
        print()
        print("  3a. Start OSRM (native):")
        print(f"       ./run_osrm.sh {region_folder} {port}")
        print()
        print("  3b. Start OSRM (docker compose) — set in .env:")
        print(f"       OSRM_REGION={region_folder}")
        print(f"       OSRM_PORT={port}")
        print(f"       docker compose up osrm -d")
    else:
        print(f"  No known region matched bbox "
              f"lat [{min_lat:.2f}, {max_lat:.2f}]  lon [{min_lon:.2f}, {max_lon:.2f}].")
        print(f"  Download the correct .osm.pbf from: {geofabrik_base}/")
        print(f"  Then run:  ./run_osrm.sh <your-region> {port}")
    print("  ────────────────────────────────────────────────────────────")


def validate_osrm_region(
    nested_urls: List[List[str]],
    osrm_base_url: str,
    sample_size: int = REGION_VALIDATION_SAMPLE_SIZE,
    snap_threshold_m: float = REGION_VALIDATION_SNAP_THRESHOLD_M,
) -> None:
    """
    Verifies that the running OSRM server covers the geographic region of the input data.

    How it works:
        Samples a small set of coordinates from the input, then queries OSRM's
        /nearest/v1/driving endpoint for each.  OSRM responds with the distance
        (in meters) from the queried coordinate to the nearest drivable road on
        the currently loaded map.

        - If the map is correct (e.g., California routes vs. California OSRM),
          the snap distance will be negligible — typically under 200 m.
        - If the map is wrong (e.g., California routes vs. Massachusetts OSRM),
          OSRM will snap to the nearest road it knows about, which will be
          hundreds or thousands of kilometers away.

    Decision rule:
        If more than half of the sampled coordinates exceed snap_threshold_m,
        the validation fails and the script exits with a descriptive error.

    Args:
        nested_urls:       Route data to sample coordinates from.
        osrm_base_url:     Base URL of the running OSRM server.
        sample_size:       Number of coordinates to probe (default: 5).
        snap_threshold_m:  Maximum acceptable snap distance in meters (default: 5,000 m).

    Raises:
        SystemExit: If the majority of sampled coordinates fall outside the loaded map.
    """
    # Collect all unique coordinates from the input, then evenly sample them
    all_coords: List[Tuple[float, float]] = []
    seen: set = set()
    for route in nested_urls:
        for url in route:
            origin, destination, waypoints = extract_coordinates_from_url(url)
            for point in [origin, destination] + waypoints:
                if point and point not in seen:
                    all_coords.append(point)
                    seen.add(point)
            if len(all_coords) >= sample_size * 20:
                break
        if len(all_coords) >= sample_size * 20:
            break

    if not all_coords:
        print("  [WARN]  Region validation skipped — no coordinates found in input.")
        return

    # Pick evenly spaced samples so we cover the full dataset spread
    step = max(1, len(all_coords) // sample_size)
    samples = all_coords[::step][:sample_size]

    # Compute bounding box of the full sample set for display
    lats = [p[0] for p in all_coords[:sample_size * 20]]
    lons = [p[1] for p in all_coords[:sample_size * 20]]
    bbox = (min(lats), max(lats), min(lons), max(lons))

    sep = "=" * 64
    thin = "-" * 64
    print(f"\n{sep}")
    print("  REGION VALIDATION")
    print(thin)
    print(f"  OSRM server      : {osrm_base_url}")
    print(f"  Input bbox       : lat [{bbox[0]:.4f}, {bbox[1]:.4f}]  "
          f"lon [{bbox[2]:.4f}, {bbox[3]:.4f}]")
    print(f"  Sampling         : {len(samples)} coordinate(s)")
    print(thin)

    failures = 0
    snap_distances: List[float] = []

    for lat, lon in samples:
        nearest_url = f"{osrm_base_url}/nearest/v1/driving/{lon},{lat}"
        try:
            resp = requests.get(nearest_url, timeout=OSRM_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == "Ok" and data.get("waypoints"):
                    snap_m = data["waypoints"][0]["distance"]
                    snap_distances.append(snap_m)
                    status = "FAIL" if snap_m > snap_threshold_m else "  OK"
                    if snap_m > snap_threshold_m:
                        failures += 1
                    print(
                        f"  [{status}]  ({lat:.5f}, {lon:.5f})  →  "
                        f"nearest road: {snap_m:>10,.1f} m"
                        + (" ← too far" if snap_m > snap_threshold_m else "")
                    )
                else:
                    failures += 1
                    print(
                        f"  [FAIL]  ({lat:.5f}, {lon:.5f})  →  "
                        f"OSRM returned no nearest road (code: {data.get('code')})"
                    )
        except requests.RequestException as exc:
            print(f"  [WARN]  Could not reach OSRM for nearest check: {exc}")
            print(f"{sep}\n")
            return  # Can't validate — let the main run surface the connection error

    print(thin)

    if snap_distances:
        avg_snap = sum(snap_distances) / len(snap_distances)
        max_snap = max(snap_distances)
        print(f"  Avg snap distance: {avg_snap:>10,.1f} m")
        print(f"  Max snap distance: {max_snap:>10,.1f} m")
        print(f"  Threshold        : {snap_threshold_m:>10,.1f} m")

    if failures > len(samples) / 2:
        print()
        print("  ERROR: The loaded OSRM map does not cover this dataset's region.")
        print("  Coordinates from the input file are far from any road in the running OSRM server.")
        print()
        print("  This usually means the wrong regional map is loaded.")
        print(f"  Input data bbox: lat [{bbox[0]:.2f}, {bbox[1]:.2f}]  "
              f"lon [{bbox[2]:.2f}, {bbox[3]:.2f}]")
        _suggest_region(bbox, osrm_base_url)
        print(f"{sep}\n")
        raise SystemExit(1)

    # Show a lightweight hint so the user can verify the region at a glance
    matched_name = "unknown"
    for (r_min_lat, r_max_lat, r_min_lon, r_max_lon), name, _ in KNOWN_REGIONS:
        if r_min_lat <= bbox[0] and bbox[1] <= r_max_lat \
                and r_min_lon <= bbox[2] and bbox[3] <= r_max_lon:
            matched_name = name
            break
    print(f"  Detected region  : {matched_name}")
    print("  Map region matches input data. Proceeding.")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# OSRM query
# ---------------------------------------------------------------------------

def get_route_distance_osrm(
    route_idx: int,
    seg_idx: int,
    url: str,
    osrm_base_url: str = "http://localhost:5002",
    retries: int = 3,
    delay: float = 2.0,
) -> SegmentResult:
    """
    Queries the local OSRM server for the road distance of a single URL segment.

    OSRM uses the Multi-Level Dijkstra (MLD) algorithm on pre-processed OpenStreetMap
    data to compute the shortest drivable path between a sequence of coordinates.
    This function converts the URL's coordinates into OSRM's expected format
    (longitude, latitude — note: reversed from Google's lat,lng order) and calls
    the /route/v1/driving endpoint.

    Fallback chain (applied when OSRM returns an unexpectedly small distance):
        1. Sum individual leg distances from the response (handles edge cases in
           OSRM's top-level distance aggregation).
        2. OSRM_ROAD_FACTOR × haversine(origin, destination) as a last resort
           approximation when no valid road distance can be obtained.

    When OSRM returns a non-OK code (e.g. NoRoute) or the HTTP request fails
    after retries, distance is approximated with OSRM_ROAD_FACTOR × sum of
    haversine legs along the full coordinate chain (same ``filtered`` list sent
    to OSRM). Those segments are not counted as *skipped* for totals.

    Args:
        route_idx:     Zero-based index of the parent route (for labeling output).
        seg_idx:       Zero-based index of this segment within its route.
        url:           Direction URL containing coordinate parameters.
        osrm_base_url: Base URL of the running OSRM server.
        retries:       Maximum number of retry attempts on connection failure.
        delay:         Seconds to wait between retries.

    Returns:
        A SegmentResult with the computed distance_km and metadata.
        skipped=True only when origin/destination are missing or fewer than two
        distinct coordinates exist after de-duplication.
    """
    origin, destination, waypoints = extract_coordinates_from_url(url)

    if not origin or not destination:
        print(
            f"  [SKIP]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
            "— missing origin or destination in URL"
        )
        return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)

    all_points: List[Tuple[float, float]] = [origin] + waypoints + [destination]

    # Remove consecutive duplicate coordinates — OSRM rejects requests with them
    filtered: List[Tuple[float, float]] = []
    for point in all_points:
        if not filtered or point != filtered[-1]:
            filtered.append(point)

    if len(filtered) < 2:
        print(
            f"  [SKIP]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
            "— not enough unique coordinates for routing"
        )
        return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)

    # OSRM expects coordinates as lon,lat (opposite of Google's lat,lng convention)
    coords_str = ";".join(f"{p[1]},{p[0]}" for p in filtered)
    osrm_url = (
        f"{osrm_base_url}/route/v1/driving/{coords_str}"
        "?overview=false&continue_straight=true"
    )

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(osrm_url, timeout=OSRM_REQUEST_TIMEOUT)

            if response.status_code == 200:
                data = response.json()

                if data.get("code") == "Ok":
                    route_data = data["routes"][0]
                    distance_km = route_data["distance"] / 1000.0

                    if distance_km < MIN_DISTANCE_KM:
                        # Fallback 1: sum individual leg distances
                        distance_km = (
                            sum(leg.get("distance", 0) for leg in route_data.get("legs", []))
                            / 1000.0
                        )

                    if distance_km < MIN_DISTANCE_KM:
                        # Fallback 2: road-factor × straight-line distance
                        distance_km = (
                            OSRM_ROAD_FACTOR * haversine_distance(origin, destination) / 1000.0
                        )

                    return SegmentResult(
                        route_idx, seg_idx, distance_km, len(waypoints), url
                    )

                # OSRM returned a non-OK code (e.g., NoRoute) — approximate path in km
                fb_km = haversine_polyline_approx_km(filtered)
                return SegmentResult(
                    route_idx,
                    seg_idx,
                    fb_km,
                    len(waypoints),
                    url,
                    skipped=False,
                    used_haversine_fallback=True,
                )

            print(
                f"  [WARN]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
                f"— OSRM HTTP {response.status_code} (attempt {attempt}/{retries})"
            )
            if response.status_code == 400:
                # Log the first and last coordinate to help diagnose malformed requests
                coord_preview = " ; ".join(
                    f"{p[0]:.5f},{p[1]:.5f}" for p in filtered[:2]
                )
                print(
                    f"         coords ({len(filtered)} pts): {coord_preview}"
                    + (" ..." if len(filtered) > 2 else "")
                )

        except requests.RequestException as exc:
            print(
                f"  [WARN]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
                f"— request failed (attempt {attempt}/{retries}): {exc}"
            )
            time.sleep(delay)

    fb_km = haversine_polyline_approx_km(filtered)
    return SegmentResult(
        route_idx,
        seg_idx,
        fb_km,
        len(waypoints),
        url,
        skipped=False,
        used_haversine_fallback=True,
    )


# ---------------------------------------------------------------------------
# Parallel processing
# ---------------------------------------------------------------------------

def calculate_total_distance_parallel(
    nested_urls: List[List[str]],
    osrm_base_url: str = "http://localhost:5002",
    max_workers: int = 6,
    input_file: str = "",
    file_stop_count: Optional[int] = None,
    coord_mode: CoordMode = "waypoints",
) -> Tuple[float, List[RouteData]]:
    """
    Calculates the total road distance across all routes, processing segments in parallel.

    Each route is a list of URL segments. Segments within and across routes are
    dispatched concurrently to the OSRM server using a thread pool, then aggregated
    into per-route and overall totals.

    Args:
        nested_urls:     List of routes; each route is a list of direction URLs.
        osrm_base_url:   Base URL of the running OSRM server.
        max_workers:     Number of concurrent threads for OSRM queries.
        input_file:      Path to the input file (used for display purposes only).
        file_stop_count: If set (e.g. from JSON), printed as the canonical Total points.
        coord_mode:      ``waypoints`` (solver-style) or ``chain`` (Amazon-style); see generate_map.

    Returns:
        (total_distance_km, route_data_list).
    """
    total_segments = sum(len(route) for route in nested_urls)
    wp_ch = delivery_coord_totals(nested_urls)

    _print_header(
        input_file,
        osrm_base_url,
        len(nested_urls),
        total_segments,
        max_workers,
        coord_mode=coord_mode,
        wp_ch=wp_ch,
    )

    validate_osrm_region(nested_urls, osrm_base_url)

    # Flatten all segments into (route_idx, seg_idx, url) tuples
    tasks: List[Tuple[int, int, str]] = [
        (r_idx, s_idx, url)
        for r_idx, route in enumerate(nested_urls)
        for s_idx, url in enumerate(route)
    ]

    print(f"\nProcessing {total_segments} segment(s) using {max_workers} parallel worker(s)...\n")

    results: Dict[int, SegmentResult] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(
                get_route_distance_osrm, r_idx, s_idx, url, osrm_base_url
            ): i
            for i, (r_idx, s_idx, url) in enumerate(tasks)
        }

        completed = 0
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            result = future.result()
            results[idx] = result
            completed += 1

            if result.skipped:
                status = "SKIP"
            elif result.used_haversine_fallback:
                status = " ~H"
            else:
                status = "  OK"
            stops = result.waypoint_count + 2  # waypoints + origin + destination
            print(
                f"  [{status}]  {completed:>3}/{total_segments}"
                f"  Route {result.route_index + 1:>2}"
                f"  Seg {result.segment_index + 1}"
                f"  {result.distance_km:>8.3f} km"
                f"  ({stops} stops)"
            )

    # Aggregate per-route totals in original order
    route_totals: List[float] = [0.0] * len(nested_urls)
    route_seg_counts: List[int] = [0] * len(nested_urls)
    for result in results.values():
        route_totals[result.route_index] += result.distance_km
        route_seg_counts[result.route_index] += 1

    total_distance = sum(route_totals)
    skipped_count = sum(1 for r in results.values() if r.skipped)
    haversine_fallback_count = sum(1 for r in results.values() if r.used_haversine_fallback)

    # Build per-route structured data (needed for stop count in summary and map generation)
    route_data_list: List[RouteData] = []
    for r_idx, route_urls in enumerate(nested_urls):
        coords = _flatten_route_coords(route_urls, coord_mode)
        depot: Optional[Tuple[float, float]] = None
        if route_urls:
            o, _, _ = extract_coordinates_from_url(route_urls[0])
            depot = o
        route_data_list.append(RouteData(
            route_index=r_idx,
            coords=coords,
            distance_km=route_totals[r_idx],
            segment_count=route_seg_counts[r_idx],
            stop_count=len(coords),
            depot=depot,
        ))

    total_stops_parsed = sum(r.stop_count for r in route_data_list)
    _print_summary(
        route_totals,
        route_seg_counts,
        total_distance,
        total_segments,
        skipped_count,
        total_stops_parsed,
        file_stop_count=file_stop_count,
        haversine_fallback_count=haversine_fallback_count,
    )

    return total_distance, route_data_list


def _flatten_route_coords(route_urls: List[str], mode: CoordMode) -> List[Tuple[float, float]]:
    """Delivery stops for one route (same rules as ``generate_map.route_to_coords``)."""
    return route_to_coords(route_urls, mode=mode)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_header(
    input_file: str,
    osrm_url: str,
    num_routes: int,
    num_segments: int,
    workers: int,
    *,
    coord_mode: CoordMode = "waypoints",
    wp_ch: Optional[Tuple[int, int]] = None,
) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print("  LAST-MILE ROUTE DISTANCE VERIFIER")
    print(sep)
    if input_file:
        print(f"  Input    : {input_file}")
    print(f"  OSRM     : {osrm_url}")
    print(f"  Routes   : {num_routes}   |   Segments: {num_segments}   |   Workers: {workers}")
    if wp_ch is not None:
        wp, ch = wp_ch
        print(
            f"  Stop mode: {coord_mode}  (waypoints-only {wp:,} | chain-merged {ch:,} delivery coords)"
        )
    else:
        print(f"  Stop mode: {coord_mode}")
    print(sep)


def _print_summary(
    route_totals: List[float],
    route_seg_counts: List[int],
    total_distance: float,
    total_segments: int,
    skipped_count: int,
    total_stops_parsed: int,
    *,
    file_stop_count: Optional[int] = None,
    haversine_fallback_count: int = 0,
) -> None:
    sep = "=" * 64
    thin = "-" * 64
    print(f"\n{sep}")
    print("  ROUTE SUMMARY")
    print(thin)
    for i, (km, segs) in enumerate(zip(route_totals, route_seg_counts)):
        seg_label = f"{segs} segment" + ("" if segs == 1 else "s")
        print(f"  Route {i + 1:>3}  |  {seg_label:<12}  |  {km:>9.3f} km")
    print(thin)
    print(f"  Total routes    : {len(route_totals):,}")
    print(f"  Total segments  : {total_segments:,}")
    if file_stop_count is not None:
        print(f"  Total points    : {file_stop_count:,}  (JSON delivery_stop_count / stop_count)")
    else:
        print(f"  Total points    : {total_stops_parsed:,}")
    if skipped_count:
        print(f"  Skipped         : {skipped_count}")
    if haversine_fallback_count:
        print(
            f"  Haversine FB    : {haversine_fallback_count} segment(s) "
            f"(OSRM NoRoute / HTTP failure; distance ≈ {OSRM_ROAD_FACTOR}× straight-line legs)"
        )
    print(f"\n  TOTAL DISTANCE  :  {total_distance:>10.3f} km")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Coordinate validation (optional sanity check)
# ---------------------------------------------------------------------------

def validate_all_coordinates_present(
    urls_orig: List[List[str]],
    urls_new: List[List[str]],
    tolerance: float = 0.0001,
) -> dict:
    """
    Verifies that every coordinate in urls_orig also appears in urls_new.

    Use this before comparing Amazon vs. solver distances to confirm both datasets
    cover the exact same set of delivery stops.

    Args:
        urls_orig:  Reference dataset (e.g., Amazon historical routes).
        urls_new:   Candidate dataset (e.g., solver-generated routes).
        tolerance:  Maximum lat/lng delta to treat two points as identical
                    (~11 meters at the equator).

    Returns:
        dict with keys:
            all_found      (bool)  — True if every stop in urls_orig was found in urls_new.
            missing        (int)   — Count of coordinates present in orig but absent in new.
            missing_coords (list)  — List of (lat, lng) tuples that are missing.
    """
    def extract_all_coords(nested: List[List[str]]) -> set:
        coords: set = set()
        for route in nested:
            for url in route:
                origin, destination, waypoints = extract_coordinates_from_url(url)
                for point in [origin, destination] + waypoints:
                    if point:
                        coords.add((round(point[0], 5), round(point[1], 5)))
        return coords

    coords_orig = extract_all_coords(urls_orig)
    coords_new = extract_all_coords(urls_new)

    missing = [
        c for c in coords_orig
        if not any(
            abs(c[0] - n[0]) < tolerance and abs(c[1] - n[1]) < tolerance
            for n in coords_new
        )
    ]

    sep = "=" * 64
    thin = "-" * 64
    print(f"\n{sep}")
    print("  COORDINATE VALIDATION")
    print(thin)
    print(f"  Reference stops  : {len(coords_orig)}")
    print(f"  Candidate stops  : {len(coords_new)}")
    print(f"  Missing          : {len(missing)}")
    if missing:
        for c in missing[:10]:
            print(f"    [MISS]  {c[0]:.5f},  {c[1]:.5f}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
    else:
        print("  All stops matched — datasets cover the same delivery points.")
    print(f"{sep}\n")

    return {
        "all_found": len(missing) == 0,
        "missing": len(missing),
        "missing_coords": missing,
    }


def run_delivery_stop_count_validation(
    nested_urls: List[List[str]],
    expected: int,
    *,
    require_match: bool,
    silent: bool = False,
) -> None:
    """
    Compare parsed stops from URLs (``generate_map.route_to_coords`` rules) to ``expected``.

    Prints a banner unless ``silent``; exits with code 1 when ``require_match`` and counts differ.
    """
    from generate_map import format_delivery_stop_validation, validate_delivery_stop_count_match

    v = validate_delivery_stop_count_match(nested_urls, expected)
    if not silent:
        sep = "=" * 64
        print(f"\n{sep}\n  DELIVERY STOP COUNT VALIDATION\n{sep}")
        print(f"  {format_delivery_stop_validation(v)}")
        print(f"{sep}\n")
    if require_match and not v.ok:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# CLI defaults from input filename
# ---------------------------------------------------------------------------


def infer_map_path_and_label(input_path: str) -> Tuple[str, str, bool]:
    """
    Default ``--map`` / ``--label`` from the input filename.

    * ``routes_result_DLA4_AMZ_197.json`` → ``maps/AMZ_DLA4.html``, ``DLA4 AMAZON (OSRM)``
    * ``routes_result_DBO1_56.json`` under ``.../solver/`` → ``maps/SOLVER_DBO1.html``, ``DBO1 SOLVER (OSRM)``
    * Other ``routes_result_<CODE>_<N>.json`` → ``maps/<CODE>.html``, ``<CODE> (OSRM)``
    * Fallback: ``maps/<STEM>.html`` from the basename (sanitized); third return ``True`` = generic.

    Returns:
        (map_path, label, generic_fallback). When ``generic_fallback`` is True, callers should
        suggest ``--map`` / ``--label`` — the stem alone may be a poor title (e.g. ``data.json``).
    """
    base = os.path.basename(input_path)
    stem, _ = os.path.splitext(base)
    norm = input_path.replace("\\", "/").lower()

    m_amz = re.match(
        r"routes_result_(?P<code>[A-Za-z0-9]+)_AMZ_(?P<num>\d+)$",
        stem,
        re.IGNORECASE,
    )
    if m_amz:
        code = m_amz.group("code").upper()
        return f"maps/AMZ_{code}.html", f"{code} AMAZON (OSRM)", False

    m_rt = re.match(
        r"routes_result_(?P<code>[A-Za-z0-9]+)_(?P<num>\d+)$",
        stem,
        re.IGNORECASE,
    )
    if m_rt:
        code = m_rt.group("code").upper()
        if "/solver/" in norm:
            return f"maps/SOLVER_{code}.html", f"{code} SOLVER (OSRM)", False
        return f"maps/{code}.html", f"{code} (OSRM)", False

    safe = re.sub(r"[^\w\-.]+", "_", stem).strip("_") or "routes"
    safe_u = safe.upper()
    return f"maps/{safe_u}.html", f"{safe_u} (OSRM)", True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify route distances against a local OSRM instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python scripts/check_distance_osrm.py --input inputs/amazon/routes_result_DLA4_AMZ_197.json
    # writes maps/AMZ_DLA4.html, title "DLA4 AMAZON (OSRM)" (omit --map / --label)
  python scripts/check_distance_osrm.py --input inputs/solver/routes.json --no-map
  python scripts/check_distance_osrm.py --input data.json --map out.html --label "My run"
    # If the file is not named routes_result_*, stderr notes generic inference; pass --map/--label.
        """,
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a JSON file containing a list-of-lists of direction URLs.",
    )
    parser.add_argument(
        "--osrm",
        default="http://localhost:5002",
        help="Base URL of the running OSRM server (default: http://localhost:5002).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of parallel worker threads for OSRM queries (default: 6).",
    )
    parser.add_argument(
        "--map",
        metavar="OUTPUT_HTML",
        default=None,
        help="HTML map output path. "
        "Default: inferred from --input basename (structured names like routes_result_*_AMZ_*.json; "
        "otherwise maps/<STEM>.html — use this flag when the filename is generic).",
    )
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Skip writing an HTML map (overrides the default inferred from --input).",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Map panel title. "
        "Default: inferred from --input for structured names; otherwise '<STEM> (OSRM)' — set explicitly for plain names.",
    )
    parser.add_argument(
        "--delivery-stops",
        type=int,
        default=None,
        metavar="N",
        help="Canonical delivery-stop total (solver truth). Overrides JSON metadata. "
        "Used for ROUTE SUMMARY Total points, map panel Total stops, and map marker padding "
        "when N exceeds coords parsed from URLs.",
    )
    parser.add_argument(
        "--coord-mode",
        choices=("auto", "waypoints", "chain"),
        default="auto",
        help="How URLs map to delivery coordinates: waypoints-only, chain-merged, or auto "
        "(JSON coord_mode / delivery_stop_count; default waypoints when absent).",
    )
    parser.add_argument(
        "--require-delivery-count-match",
        action="store_true",
        help="Exit before OSRM if delivery_stop_count (or aliases) != stops parsed from routes URLs.",
    )
    parser.add_argument(
        "--skip-delivery-count-validation",
        action="store_true",
        help="Skip delivery-stop validation entirely unless combined with --require-delivery-count-match "
        "(then validate silently and exit 1 on mismatch).",
    )
    args = parser.parse_args()

    inferred_map, inferred_label, map_infer_generic = infer_map_path_and_label(args.input)
    if args.no_map:
        map_output_path: Optional[str] = None
    elif args.map is not None:
        map_output_path = args.map
    else:
        map_output_path = inferred_map

    map_label = args.label if args.label is not None else inferred_label

    if map_infer_generic and map_output_path and not args.no_map:
        parts = []
        if args.map is None:
            parts.append(f"map → {map_output_path}")
        if args.label is None:
            parts.append(f"title → {map_label!r}")
        if parts:
            print(
                "[check_distance_osrm] Input basename does not match routes_result_<CODE>_AMZ_<n>.json "
                "or routes_result_<CODE>_<n>.json; inferred "
                f"{' and '.join(parts)}. For clearer outputs use explicit --map and --label.",
                file=sys.stderr,
            )

    with open(args.input) as f:
        raw = json.load(f)

    try:
        nested_urls, file_stop_count, doc_coord_mode = load_routes_json_payload(raw)
    except ValueError as e:
        raise ValueError(f"Input JSON: {e}") from e

    if not isinstance(nested_urls, list) or not nested_urls:
        raise ValueError("Input JSON must be a non-empty list of route URL lists.")

    # Canonical stop total for summary + map: CLI overrides JSON (solver truth).
    report_stop_count: Optional[int] = (
        args.delivery_stops if args.delivery_stops is not None else file_stop_count
    )
    canonical_for_mode = (
        args.delivery_stops if args.delivery_stops is not None else file_stop_count
    )
    coord_mode = resolve_delivery_coord_mode(
        nested_urls,
        canonical=canonical_for_mode,
        force=args.coord_mode,  # type: ignore[arg-type]
        doc_coord_mode=doc_coord_mode,
    )

    if file_stop_count is not None:
        if args.skip_delivery_count_validation and not args.require_delivery_count_match:
            pass
        else:
            run_delivery_stop_count_validation(
                nested_urls,
                file_stop_count,
                require_match=args.require_delivery_count_match,
                silent=bool(
                    args.skip_delivery_count_validation and args.require_delivery_count_match
                ),
            )

    total_km, route_data_list = calculate_total_distance_parallel(
        nested_urls,
        osrm_base_url=args.osrm,
        max_workers=args.workers,
        input_file=args.input,
        file_stop_count=report_stop_count,
        coord_mode=coord_mode,
    )

    if map_output_path:
        total_stops_parsed = sum(r.stop_count for r in route_data_list)
        panel_report_total_stops = (
            report_stop_count if report_stop_count is not None else total_stops_parsed
        )

        out_dir = os.path.dirname(os.path.abspath(map_output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # Auto-generate a contextual map description from the label keyword
        label_lower = map_label.lower()
        if any(k in label_lower for k in ("amazon", "amz", "historical", "driver")):
            map_description = (
                "Historical Amazon driver routes reconstructed from GPS tracking data. "
                "Each dot marks one delivery stop. "
                "Route lines are not displayed as only stop coordinates are available. "
                "Distances verified against the road network using OSRM."
            )
        elif any(k in label_lower for k in ("solver", "opt", "optimized", "solution")):
            map_description = (
                "Optimizer-generated routes covering the same delivery stops as the Amazon baseline. "
                "Routes were planned under constraints: stops per route, weight capacity, volume capacity, "
                "and delivery time windows. Delivery volumes were used as solver inputs but are not visualized here. "
                "Route lines are not displayed; only stop coordinates are available. "
                "Distances verified using OSRM."
            )
        else:
            map_description = ""

        print(f"Generating map → {map_output_path} …")
        build_map_from_route_data(
            route_data_list,
            total_km,
            label=map_label,
            description=map_description,
            output_path=map_output_path,
            display_stop_count=report_stop_count,
            panel_report_total_stops=panel_report_total_stops,
        )
        print(f"Map saved      → {map_output_path}")
