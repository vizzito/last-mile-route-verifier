"""
generate_map.py
===============
Generates an interactive HTML map from a route JSON file.

Uses the same input format as check_distance_osrm.py — a list of routes,
where each route is a list of Google Maps direction URLs. Coordinates are
parsed directly from the URLs; no API calls are made.

Usage:
    python scripts/generate_map.py --input inputs/amazon/routes.json
    python scripts/generate_map.py --input inputs/solver/routes.json --output maps/solver.html
    python scripts/generate_map.py \
        --input inputs/amazon/routes.json \
        --compare inputs/solver/routes.json \
        --output maps/comparison.html

What each view shows:
    Single input  : all routes plotted with different colors, numbered stops.
    --compare     : two datasets side-by-side on the same map (tabs).

Requirements:
    pip install -r requirements.txt
"""

import argparse
import json
import math
import re
import sys
from itertools import cycle
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import folium
    from folium.plugins import GroupedLayerControl
except ImportError:
    print("ERROR: folium is required. Run: pip install folium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Colour palette (distinct, colourblind-friendly-ish)
# ---------------------------------------------------------------------------

ROUTE_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9a6324", "#fffac8", "#800000", "#aaffc3",
    "#808000", "#ffd8b1", "#000075", "#a9a9a9", "#ffffff",
]


# ---------------------------------------------------------------------------
# URL parsing (same logic as check_distance_osrm.py — no shared dep)
# ---------------------------------------------------------------------------

def extract_coordinates_from_url(
    url: str,
) -> Tuple[
    Optional[Tuple[float, float]],
    Optional[Tuple[float, float]],
    List[Tuple[float, float]],
]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    def parse_coord(s):
        if not s:
            return None
        try:
            lat, lng = map(float, s.split(","))
            return (lat, lng)
        except (ValueError, AttributeError):
            return None

    origin = parse_coord(qs.get("origin", [None])[-1])
    destination = parse_coord(qs.get("destination", [None])[-1])

    waypoints = []
    wp_str = qs.get("waypoints", [None])[-1]
    if wp_str:
        wp_str = re.sub(r"optimize:(true|false)\|", "", wp_str, count=1)
        for wp in wp_str.split("|"):
            coord = parse_coord(wp.strip())
            if coord:
                waypoints.append(coord)

    return origin, destination, waypoints


def route_to_coords(route_urls: List[str]) -> List[Tuple[float, float]]:
    """Flatten all coordinates in a route (preserving order, no consecutive dups)."""
    coords = []
    for url in route_urls:
        origin, destination, waypoints = extract_coordinates_from_url(url)
        segment_points = []
        if origin:
            segment_points.append(origin)
        segment_points.extend(waypoints)
        if destination:
            segment_points.append(destination)
        for p in segment_points:
            if not coords or coords[-1] != p:
                coords.append(p)
    return coords


# ---------------------------------------------------------------------------
# Map building
# ---------------------------------------------------------------------------

def centroid(all_coords: List[Tuple[float, float]]) -> Tuple[float, float]:
    lats = [c[0] for c in all_coords]
    lngs = [c[1] for c in all_coords]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


def add_routes_to_map(
    fmap: folium.Map,
    nested_urls: List[List[str]],
    label_prefix: str = "",
    layer_group: Optional[folium.FeatureGroup] = None,
    show_stop_numbers: bool = True,
) -> None:
    """
    Draws each route as a coloured polyline with numbered stop markers.
    Routes are added to `layer_group` if provided, otherwise directly to `fmap`.
    """
    colors = cycle(ROUTE_COLORS)
    target = layer_group if layer_group is not None else fmap

    for route_idx, route_urls in enumerate(nested_urls):
        color = next(colors)
        coords = route_to_coords(route_urls)
        if not coords:
            continue

        route_label = f"{label_prefix}Route {route_idx + 1} ({len(coords)} stops)"

        # Polyline
        folium.PolyLine(
            locations=coords,
            color=color,
            weight=3,
            opacity=0.85,
            tooltip=route_label,
        ).add_to(target)

        # Stop markers
        for stop_idx, (lat, lng) in enumerate(coords):
            is_first = stop_idx == 0
            is_last = stop_idx == len(coords) - 1

            if is_first or is_last:
                # Depot / end: filled circle
                folium.CircleMarker(
                    location=(lat, lng),
                    radius=7,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=1.0,
                    tooltip=f"{route_label} — {'START' if is_first else 'END'}",
                ).add_to(target)
            elif show_stop_numbers:
                folium.CircleMarker(
                    location=(lat, lng),
                    radius=4,
                    color=color,
                    fill=True,
                    fill_color="white",
                    fill_opacity=0.9,
                    tooltip=f"{route_label} — Stop {stop_idx}",
                ).add_to(target)


def build_single_map(nested_urls: List[List[str]], title: str = "Routes") -> folium.Map:
    """Builds a map for a single dataset."""
    all_coords = []
    for route_urls in nested_urls:
        all_coords.extend(route_to_coords(route_urls))

    if not all_coords:
        raise ValueError("No valid coordinates found in input.")

    center = centroid(all_coords)
    fmap = folium.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    # Title
    title_html = f"""
    <div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:8px 16px;border-radius:6px;
                border:1px solid #ccc;font-family:sans-serif;font-size:14px;
                font-weight:bold;z-index:9999;box-shadow:2px 2px 6px rgba(0,0,0,.2)">
        {title} &nbsp;·&nbsp; {len(nested_urls)} routes &nbsp;·&nbsp; {len(all_coords)} stops
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(title_html))

    add_routes_to_map(fmap, nested_urls)
    return fmap


def build_comparison_map(
    urls_a: List[List[str]],
    urls_b: List[List[str]],
    label_a: str = "Dataset A",
    label_b: str = "Dataset B",
) -> folium.Map:
    """Builds a side-by-side comparison map with layer toggle."""
    all_coords = []
    for route_urls in urls_a + urls_b:
        all_coords.extend(route_to_coords(route_urls))

    if not all_coords:
        raise ValueError("No valid coordinates found in inputs.")

    center = centroid(all_coords)
    fmap = folium.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    def total_stops(nested):
        return sum(len(route_to_coords(r)) for r in nested)

    # Title
    title_html = f"""
    <div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);
                background:white;padding:8px 16px;border-radius:6px;
                border:1px solid #ccc;font-family:sans-serif;font-size:14px;
                font-weight:bold;z-index:9999;box-shadow:2px 2px 6px rgba(0,0,0,.2)">
        Comparison: {label_a} vs {label_b}
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(title_html))

    group_a = folium.FeatureGroup(name=f"{label_a} ({len(urls_a)} routes, {total_stops(urls_a)} stops)", show=True)
    group_b = folium.FeatureGroup(name=f"{label_b} ({len(urls_b)} routes, {total_stops(urls_b)} stops)", show=True)

    add_routes_to_map(fmap, urls_a, label_prefix=f"[{label_a}] ", layer_group=group_a)
    add_routes_to_map(fmap, urls_b, label_prefix=f"[{label_b}] ", layer_group=group_b)

    group_a.add_to(fmap)
    group_b.add_to(fmap)
    folium.LayerControl(collapsed=False).add_to(fmap)

    return fmap


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HTML route map from JSON input")
    parser.add_argument("--input", required=True, help="Primary JSON file (list of route URL lists)")
    parser.add_argument("--compare", help="Optional second JSON file to overlay as a comparison layer")
    parser.add_argument("--output", default="map.html", help="Output HTML file path (default: map.html)")
    parser.add_argument("--label-a", default="Amazon", help="Label for --input dataset (default: Amazon)")
    parser.add_argument("--label-b", default="Solver", help="Label for --compare dataset (default: Solver)")
    args = parser.parse_args()

    with open(args.input) as f:
        urls_a = json.load(f)

    if args.compare:
        with open(args.compare) as f:
            urls_b = json.load(f)
        fmap = build_comparison_map(urls_a, urls_b, label_a=args.label_a, label_b=args.label_b)
        print(f"Comparison map: {len(urls_a)} routes vs {len(urls_b)} routes")
    else:
        fmap = build_single_map(urls_a, title=args.label_a)
        print(f"Map: {len(urls_a)} routes")

    fmap.save(args.output)
    print(f"Saved → {args.output}")
