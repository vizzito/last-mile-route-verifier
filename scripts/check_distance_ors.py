"""
check_distance_ors.py
=====================
Verifies total route distances using the OpenRouteService (ORS) online routing API.

Accepts the same JSON input format as check_distance_osrm.py — no Docker or local
map files required. Useful as a cross-check against the OSRM verifier, or when
local infrastructure is not available.

Requires a free ORS API key (https://openrouteservice.org/dev/#/signup).
Set it in your environment before running:

    export ORS_API_KEY=your_key_here
    python scripts/check_distance_ors.py --input inputs/amazon/routes.json

Rate limits (free tier):
    ~40 requests/minute, ~500 requests/day.
    Use --wait to control the delay between requests (default: 1.6s ≈ 37 req/min).
    Paid tiers support higher throughput — lower --wait accordingly.

Processing is intentionally sequential (no parallel workers) to respect rate limits.

Usage:
    python scripts/check_distance_ors.py --input inputs/amazon/routes.json
    python scripts/check_distance_ors.py --input inputs/solver/routes.json --wait 1.0

Requirements:
    pip install -r requirements.txt
    ORS_API_KEY environment variable set
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORS_API_URL: str = "https://api.openrouteservice.org/v2/directions/driving-car"
ORS_REQUEST_TIMEOUT: int = 15       # HTTP timeout in seconds
DEFAULT_WAIT_S: float = 1.6         # Default delay between requests (free tier safe)
DEFAULT_RETRIES: int = 3            # Max retry attempts per segment


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SegmentResult:
    """Holds the distance result for a single URL segment queried from ORS."""
    route_index: int
    segment_index: int
    distance_km: float
    waypoint_count: int
    url: str
    skipped: bool = False


# ---------------------------------------------------------------------------
# URL parsing (same logic as check_distance_osrm.py)
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

    Parses the standard query parameters:
        origin      = LAT,LNG
        destination = LAT,LNG
        waypoints   = LAT,LNG|LAT,LNG|...

    Strips the optimization flag prefix (e.g., "optimize:true|") from waypoints.

    Returns:
        (origin, destination, waypoints) as (lat, lng) float tuples.
        Elements are None / empty list if absent or malformed.
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
        waypoints_str = re.sub(r"optimize:(true|false)\|", "", waypoints_str, count=1)
        for wp in waypoints_str.split("|"):
            coord = parse_coord(wp.strip())
            if coord:
                waypoints.append(coord)

    return origin, destination, waypoints


# ---------------------------------------------------------------------------
# Input JSON loading (same format as check_distance_osrm.py)
# ---------------------------------------------------------------------------

def load_routes_json_payload(
    raw,
) -> Tuple[List[List[str]], Optional[int]]:
    """
    Accepts either a bare list of route URL lists, or a JSON object:
        {"routes": [...], "delivery_stop_count": N}

    Aliases for the count field: optimized_waypoints_count, stop_count.

    Returns:
        (nested_urls, delivery_stop_count)
    """
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        routes = raw.get("routes")
        if routes is None:
            raise ValueError("JSON object must include a 'routes' key.")
        n = (
            raw.get("delivery_stop_count")
            or raw.get("optimized_waypoints_count")
            or raw.get("stop_count")
        )
        return routes, (int(n) if n is not None else None)
    raise ValueError("JSON root must be a list or an object with 'routes'.")


# ---------------------------------------------------------------------------
# ORS query
# ---------------------------------------------------------------------------

def get_route_distance_ors(
    route_idx: int,
    seg_idx: int,
    url: str,
    api_key: str,
    retries: int = DEFAULT_RETRIES,
) -> SegmentResult:
    """
    Queries OpenRouteService for the road distance of a single URL segment.

    ORS uses a POST request with coordinates in [longitude, latitude] order.
    The response includes a `summary.distance` field in meters.

    On HTTP 429 (rate limit exceeded): applies exponential backoff before retrying.
    On other failures: logs the error and returns skipped=True.

    Args:
        route_idx:  Zero-based index of the parent route (for labeling).
        seg_idx:    Zero-based index of this segment within its route.
        url:        Direction URL containing coordinate parameters.
        api_key:    ORS API key.
        retries:    Maximum retry attempts.

    Returns:
        SegmentResult with distance_km and metadata.
    """
    origin, destination, waypoints = extract_coordinates_from_url(url)

    if not origin or not destination:
        print(
            f"  [SKIP]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
            "— missing origin or destination"
        )
        return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)

    all_points: List[Tuple[float, float]] = [origin] + waypoints + [destination]

    # Remove consecutive duplicates — ORS may reject them
    filtered: List[Tuple[float, float]] = []
    for point in all_points:
        if not filtered or point != filtered[-1]:
            filtered.append(point)

    if len(filtered) < 2:
        print(
            f"  [SKIP]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
            "— not enough unique coordinates"
        )
        return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)

    # ORS expects [longitude, latitude] — opposite of Google's lat,lng convention
    coordinates = [[p[1], p[0]] for p in filtered]

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    payload = {"coordinates": coordinates}

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                ORS_API_URL, json=payload, headers=headers, timeout=ORS_REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                data = response.json()
                if "routes" in data and data["routes"]:
                    distance_km = data["routes"][0]["summary"]["distance"] / 1000.0
                    return SegmentResult(
                        route_idx, seg_idx, distance_km, len(waypoints), url
                    )
                print(
                    f"  [WARN]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
                    f"— unexpected ORS response: {list(data.keys())}"
                )
                return SegmentResult(route_idx, seg_idx, 0.0, len(waypoints), url, skipped=True)

            if response.status_code == 429:
                # Rate limit — exponential backoff
                wait_s = attempt * 10
                print(
                    f"  [WAIT]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
                    f"— rate limited (429), waiting {wait_s}s before retry {attempt}/{retries}"
                )
                time.sleep(wait_s)
                continue

            print(
                f"  [WARN]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
                f"— ORS HTTP {response.status_code} (attempt {attempt}/{retries})"
            )
            if response.status_code == 400:
                coord_preview = " ; ".join(
                    f"{p[0]:.5f},{p[1]:.5f}" for p in filtered[:2]
                )
                print(
                    f"         coords ({len(filtered)} pts): {coord_preview}"
                    + (" ..." if len(filtered) > 2 else "")
                )

        except requests.RequestException as exc:
            print(
                f"  [WARN]  Route {route_idx + 1:>4}  Seg {seg_idx + 1:>2}  "
                f"— request failed (attempt {attempt}/{retries}): {exc}"
            )

    return SegmentResult(route_idx, seg_idx, 0.0, 0, url, skipped=True)


# ---------------------------------------------------------------------------
# Sequential processing
# ---------------------------------------------------------------------------

def calculate_total_distance_sequential(
    nested_urls: List[List[str]],
    api_key: str,
    wait_s: float = DEFAULT_WAIT_S,
    retries: int = DEFAULT_RETRIES,
    input_file: str = "",
    file_stop_count: Optional[int] = None,
) -> float:
    """
    Calculates total road distance across all routes, querying ORS sequentially.

    Sequential processing (no thread pool) is intentional — ORS free-tier rate
    limits make parallelism counterproductive. Each request is followed by a
    configurable delay to stay within quota.

    Args:
        nested_urls:    List of routes; each route is a list of direction URLs.
        api_key:        ORS API key.
        wait_s:         Seconds to wait between requests.
        retries:        Max retry attempts per segment.
        input_file:     Input file path (displayed in header).
        file_stop_count: Canonical stop count from JSON metadata (for display).

    Returns:
        Total road distance in kilometers.
    """
    total_segments = sum(len(route) for route in nested_urls)
    _print_header(input_file, len(nested_urls), total_segments, wait_s)

    print(
        f"\nProcessing {total_segments} segment(s) sequentially "
        f"(~{wait_s}s between requests)...\n"
    )

    results: List[SegmentResult] = []
    completed = 0

    for r_idx, route in enumerate(nested_urls):
        for s_idx, url in enumerate(route):
            result = get_route_distance_ors(r_idx, s_idx, url, api_key, retries)
            results.append(result)
            completed += 1

            status = "SKIP" if result.skipped else "  OK"
            stops = result.waypoint_count + 2
            print(
                f"  [{status}]  {completed:>4}/{total_segments}"
                f"  Route {result.route_index + 1:>4}"
                f"  Seg {result.segment_index + 1}"
                f"  {result.distance_km:>8.3f} km"
                f"  ({stops} stops)"
            )

            # Respect rate limit — sleep after every request except the last
            if completed < total_segments:
                time.sleep(wait_s)

    # Aggregate per-route totals
    route_totals: List[float] = [0.0] * len(nested_urls)
    route_seg_counts: List[int] = [0] * len(nested_urls)
    for result in results:
        route_totals[result.route_index] += result.distance_km
        route_seg_counts[result.route_index] += 1

    total_distance = sum(route_totals)
    skipped_count = sum(1 for r in results if r.skipped)

    _print_summary(
        route_totals, route_seg_counts, total_distance,
        total_segments, skipped_count, file_stop_count,
    )

    return total_distance


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_header(
    input_file: str,
    num_routes: int,
    num_segments: int,
    wait_s: float,
) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print("  LAST-MILE ROUTE DISTANCE VERIFIER  [OpenRouteService]")
    print(sep)
    if input_file:
        print(f"  Input    : {input_file}")
    print(f"  ORS API  : {ORS_API_URL}")
    print(
        f"  Routes   : {num_routes}   |   "
        f"Segments: {num_segments}   |   "
        f"Wait: {wait_s}s/req"
    )
    print(sep)


def _print_summary(
    route_totals: List[float],
    route_seg_counts: List[int],
    total_distance: float,
    total_segments: int,
    skipped_count: int,
    file_stop_count: Optional[int],
) -> None:
    sep = "=" * 64
    thin = "-" * 64
    print(f"\n{sep}")
    print("  ROUTE SUMMARY")
    print(thin)
    for i, (km, segs) in enumerate(zip(route_totals, route_seg_counts)):
        seg_label = f"{segs} segment" + ("" if segs == 1 else "s")
        print(f"  Route {i + 1:>4}  |  {seg_label:<12}  |  {km:>9.3f} km")
    print(thin)
    print(f"  Total routes    : {len(route_totals):,}")
    print(f"  Total segments  : {total_segments:,}")
    if file_stop_count is not None:
        print(f"  Total points    : {file_stop_count:,}")
    if skipped_count:
        print(f"  Skipped         : {skipped_count}")
    print(f"\n  TOTAL DISTANCE  :  {total_distance:>10.3f} km")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify route distances using the OpenRouteService online API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python scripts/check_distance_ors.py --input inputs/amazon/routes.json
  python scripts/check_distance_ors.py --input inputs/solver/routes.json --wait 1.0

environment:
  ORS_API_KEY  Your OpenRouteService API key (required).
               Get a free key at https://openrouteservice.org/dev/#/signup
        """,
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to a JSON file with a list-of-lists of direction URLs "
             "(same format as check_distance_osrm.py).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_S,
        metavar="SECONDS",
        help=f"Seconds to wait between requests (default: {DEFAULT_WAIT_S}). "
             "Free tier: keep ≥ 1.5. Paid tiers can use lower values.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Max retry attempts per segment on failure (default: {DEFAULT_RETRIES}).",
    )
    args = parser.parse_args()

    # Require API key from environment
    api_key = os.environ.get("ORS_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: ORS_API_KEY environment variable is not set.\n"
            "\n"
            "  Get a free key at: https://openrouteservice.org/dev/#/signup\n"
            "\n"
            "  Then set it before running:\n"
            "    export ORS_API_KEY=your_key_here\n"
            "    python scripts/check_distance_ors.py --input inputs/amazon/routes.json",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(args.input) as f:
        raw = json.load(f)

    nested_urls, file_stop_count = load_routes_json_payload(raw)

    if not isinstance(nested_urls, list) or not nested_urls:
        raise ValueError("Input JSON must be a non-empty list of route URL lists.")

    calculate_total_distance_sequential(
        nested_urls,
        api_key=api_key,
        wait_s=args.wait,
        retries=args.retries,
        input_file=args.input,
        file_stop_count=file_stop_count,
    )
