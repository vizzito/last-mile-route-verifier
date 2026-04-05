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
        --input inputs/amazon/routes.json \\
        --osrm  http://localhost:5002 \\
        --workers 6

Requirements:
    pip install -r requirements.txt
    OSRM server running locally (see docker-compose.yml or run_osrm.sh)
"""

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EARTH_RADIUS_M: float = 6_371_000    # Mean Earth radius in meters
OSRM_ROAD_FACTOR: float = 1.5        # Straight-line to road-distance multiplier (haversine fallback)
MIN_DISTANCE_KM: float = 0.01        # Minimum plausible segment distance in km
OSRM_REQUEST_TIMEOUT: int = 10       # HTTP request timeout in seconds


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SegmentResult:
    """Holds the distance result for a single URL segment queried from OSRM."""
    route_index: int
    segment_index: int
    distance_km: float
    waypoint_count: int
    url: str
    skipped: bool = False


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

    Args:
        route_idx:     Zero-based index of the parent route (for labeling output).
        seg_idx:       Zero-based index of this segment within its route.
        url:           Direction URL containing coordinate parameters.
        osrm_base_url: Base URL of the running OSRM server.
        retries:       Maximum number of retry attempts on connection failure.
        delay:         Seconds to wait between retries.

    Returns:
        A SegmentResult with the computed distance_km and metadata.
        Returns distance_km=0.0 and skipped=True if the segment cannot be routed.
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

                # OSRM returned a non-OK code (e.g., NoRoute)
                return SegmentResult(route_idx, seg_idx, 0.0, len(waypoints), url, skipped=True)

            print(
                f"  [WARN]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
                f"— OSRM HTTP {response.status_code} (attempt {attempt}/{retries})"
            )

        except requests.RequestException as exc:
            print(
                f"  [WARN]  Route {route_idx + 1:>2}  Seg {seg_idx + 1:>2}  "
                f"— request failed (attempt {attempt}/{retries}): {exc}"
            )
            time.sleep(delay)

    return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)


# ---------------------------------------------------------------------------
# Parallel processing
# ---------------------------------------------------------------------------

def calculate_total_distance_parallel(
    nested_urls: List[List[str]],
    osrm_base_url: str = "http://localhost:5002",
    max_workers: int = 6,
    input_file: str = "",
) -> float:
    """
    Calculates the total road distance across all routes, processing segments in parallel.

    Each route is a list of URL segments. Segments within and across routes are
    dispatched concurrently to the OSRM server using a thread pool, then aggregated
    into per-route and overall totals.

    Args:
        nested_urls:   List of routes; each route is a list of direction URLs.
        osrm_base_url: Base URL of the running OSRM server.
        max_workers:   Number of concurrent threads for OSRM queries.
        input_file:    Path to the input file (used for display purposes only).

    Returns:
        Total road distance in kilometers.
    """
    total_segments = sum(len(route) for route in nested_urls)

    _print_header(input_file, osrm_base_url, len(nested_urls), total_segments, max_workers)

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

            status = "SKIP" if result.skipped else "  OK"
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

    _print_summary(route_totals, route_seg_counts, total_distance, total_segments, skipped_count)

    return total_distance


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_header(
    input_file: str,
    osrm_url: str,
    num_routes: int,
    num_segments: int,
    workers: int,
) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print("  LAST-MILE ROUTE DISTANCE VERIFIER")
    print(sep)
    if input_file:
        print(f"  Input    : {input_file}")
    print(f"  OSRM     : {osrm_url}")
    print(f"  Routes   : {num_routes}   |   Segments: {num_segments}   |   Workers: {workers}")
    print(sep)


def _print_summary(
    route_totals: List[float],
    route_seg_counts: List[int],
    total_distance: float,
    total_segments: int,
    skipped_count: int,
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
    print(f"  Total routes    : {len(route_totals)}")
    print(f"  Total segments  : {total_segments}")
    if skipped_count:
        print(f"  Skipped         : {skipped_count}")
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify route distances against a local OSRM instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python scripts/check_distance_osrm.py --input inputs/amazon/routes.json
  python scripts/check_distance_osrm.py --input inputs/solver/routes.json --osrm http://localhost:5002 --workers 8
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
    args = parser.parse_args()

    with open(args.input) as f:
        nested_urls = json.load(f)

    if not isinstance(nested_urls, list) or not nested_urls:
        raise ValueError("Input JSON must be a non-empty list of route URL lists.")

    calculate_total_distance_parallel(
        nested_urls,
        osrm_base_url=args.osrm,
        max_workers=args.workers,
        input_file=args.input,
    )
