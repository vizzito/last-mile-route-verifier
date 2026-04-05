"""
check_distance_osrm.py
======================
Recalculates total route distances using a local OSRM instance.

Given a JSON file with a list of Google Maps route URLs (one list per route),
this script queries OSRM for the actual road distance of each segment and
reports the total in km.

Usage:
    python scripts/check_distance_osrm.py --input samples/routes.json [--osrm http://localhost:5002]

Input JSON format:
    A list of routes, where each route is a list of Google Maps direction URLs:
    [
        ["https://www.google.com/maps/dir/?api=1&origin=...&destination=...&waypoints=..."],
        ...
    ]

Requirements:
    pip install requests
    OSRM running locally (see run_osrm.sh)
"""

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse


# ---------------------------------------------------------------------------
# Haversine (inline, no external deps)
# ---------------------------------------------------------------------------

def haversine_distance(point_a, point_b):
    """Returns road-distance approximation in meters using the haversine formula."""
    R = 6371000  # Earth radius in meters
    lat1, lon1 = math.radians(point_a[0]), math.radians(point_a[1])
    lat2, lon2 = math.radians(point_b[0]), math.radians(point_b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def extract_coordinates_from_url(url):
    """Extracts origin, destination and waypoints from a Google Maps direction URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    origin_str = qs.get('origin', [None])[-1]
    dest_str = qs.get('destination', [None])[-1]
    waypoints_str = qs.get('waypoints', [None])[-1]

    def parse_coord(s):
        if not s:
            return None
        try:
            lat, lng = map(float, s.split(','))
            return (lat, lng)
        except Exception:
            return None

    origin = parse_coord(origin_str)
    destination = parse_coord(dest_str)

    waypoints = []
    if waypoints_str:
        waypoints_str = re.sub(r'optimize:(true|false)\|', '', waypoints_str, count=1)
        for wp in waypoints_str.split('|'):
            coord = parse_coord(wp.strip())
            if coord:
                waypoints.append(coord)

    return origin, destination, waypoints


# ---------------------------------------------------------------------------
# OSRM query
# ---------------------------------------------------------------------------

def get_route_distance_osrm(url, osrm_base_url="http://localhost:5002", retries=3, delay=2):
    """Queries OSRM for the road distance (km) of a single Google Maps URL."""
    origin, destination, waypoints = extract_coordinates_from_url(url)
    if not origin or not destination:
        print(f"  [SKIP] URL missing origin/destination: {url[:80]}...")
        return 0.0, url

    all_points = [origin] + waypoints + [destination]
    # Remove consecutive duplicates
    filtered = []
    for p in all_points:
        if not filtered or p != filtered[-1]:
            filtered.append(p)

    if len(filtered) < 2:
        print("  [ERROR] Not enough valid points for routing.")
        return 0.0, url

    # OSRM expects lon,lat
    coords_str = ";".join(f"{p[1]},{p[0]}" for p in filtered)
    osrm_url = f"{osrm_base_url}/route/v1/driving/{coords_str}?overview=false&continue_straight=true"

    for attempt in range(1, retries + 1):
        try:
            import requests
            response = requests.get(osrm_url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "Ok":
                    total_distance = data["routes"][0]["distance"] / 1000  # meters → km

                    if total_distance < 0.01:
                        legs = data["routes"][0].get("legs", [])
                        total_distance = sum(leg.get("distance", 0) for leg in legs) / 1000

                    if total_distance < 0.01:
                        # Fallback: straight-line distance with road factor
                        total_distance = 1.5 * haversine_distance(origin, destination) / 1000

                    return total_distance, url
                else:
                    return 0.0, url
            else:
                print(f"  [WARN] OSRM returned HTTP {response.status_code} (attempt {attempt})")
        except Exception as e:
            print(f"  [WARN] OSRM request failed (attempt {attempt}): {e}")
            time.sleep(delay)

    return 0.0, url


# ---------------------------------------------------------------------------
# Parallel distance calculation
# ---------------------------------------------------------------------------

def calculate_total_distance_parallel(nested_urls, osrm_base_url="http://localhost:5002", max_workers=6):
    """
    Given a list-of-lists of Google Maps URLs (one list per route),
    returns the total road distance in km.
    """
    all_urls = [url for route in nested_urls for url in route]
    total_distance = 0.0

    print(f"\nProcessing {len(all_urls)} URL segments with {max_workers} workers...\n")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(
            lambda url: get_route_distance_osrm(url, osrm_base_url),
            all_urls
        ))

    for i, (distance, url) in enumerate(results, 1):
        print(f"  Segment {i:>4}/{len(all_urls)}: {distance:>8.2f} km  |  {url[:80]}...")
        total_distance += distance

    print("\n" + "=" * 60)
    print(f"  Segments processed : {len(all_urls)}")
    print(f"  Total distance     : {total_distance:,.2f} km")
    print("=" * 60)

    return total_distance


# ---------------------------------------------------------------------------
# Coordinate validation (sanity check)
# ---------------------------------------------------------------------------

def validate_all_coordinates_present(urls_orig, urls_new, tolerance=0.0001):
    """
    Verifies that every coordinate in urls_orig also appears in urls_new,
    ensuring both datasets cover the same stops.
    """
    def extract_all_coords(nested):
        coords = set()
        for route in nested:
            for url in route:
                origin, destination, waypoints = extract_coordinates_from_url(url)
                for p in ([origin, destination] + waypoints):
                    if p:
                        coords.add((round(p[0], 5), round(p[1], 5)))
        return coords

    coords_orig = extract_all_coords(urls_orig)
    coords_new = extract_all_coords(urls_new)

    missing = [
        c for c in coords_orig
        if not any(abs(c[0] - n[0]) < tolerance and abs(c[1] - n[1]) < tolerance for n in coords_new)
    ]

    print("\n" + "=" * 60)
    print("COORDINATE VALIDATION")
    print(f"  Coords in reference : {len(coords_orig)}")
    print(f"  Coords in candidate : {len(coords_new)}")
    print(f"  Missing             : {len(missing)}")
    if missing:
        for c in missing[:10]:
            print(f"    - {c[0]:.5f}, {c[1]:.5f}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10} more")
    else:
        print("  All coordinates matched.")
    print("=" * 60)

    return {"all_found": len(missing) == 0, "missing": len(missing), "missing_coords": missing}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify route distances via local OSRM")
    parser.add_argument("--input", required=True, help="JSON file with list-of-lists of Google Maps URLs")
    parser.add_argument("--osrm", default="http://localhost:5002", help="OSRM base URL (default: http://localhost:5002)")
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers (default: 6)")
    args = parser.parse_args()

    with open(args.input) as f:
        nested_urls = json.load(f)

    if not isinstance(nested_urls, list) or not nested_urls:
        raise ValueError("Input JSON must be a non-empty list of route URL lists.")

    calculate_total_distance_parallel(nested_urls, osrm_base_url=args.osrm, max_workers=args.workers)
