"""
generate_map.py
===============
Generates an interactive HTML map from a route JSON file.

Uses the same input format as check_distance_osrm.py — a list of routes,
where each route is a list of Google Maps direction URLs; or a JSON object
``{"routes": [...], "delivery_stop_count": N}`` so the map can show N markers
when several solver stops share the same encoded lat/lng. Optional
``"coord_mode": "waypoints" | "chain"`` fixes stop semantics when metadata is absent
(see ``resolve_delivery_coord_mode``).

Coordinates are parsed from the URLs; no geocoding API is called.

Usage:
    python scripts/generate_map.py --input inputs/amazon/routes.json
    python scripts/generate_map.py --input inputs/solver/routes.json --output maps/solver.html
    python scripts/generate_map.py \
        --input inputs/amazon/routes.json \
        --compare inputs/solver/routes.json \
        --output maps/comparison.html

What each view shows:
    Single input  : circle delivery markers + square depot markers (origin of each route's first URL).
    --compare     : two datasets on the same map with layer toggle (depots included per dataset).

Data source:
    Coordinates are read from the Google Maps direction URLs in the JSON file.
    Nothing is geocoded and no routing API is called.

Requirements:
    pip install -r requirements.txt
"""

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, List, Literal, Optional, Tuple, Union

CoordMode = Literal["waypoints", "chain"]
CoordModeSpec = Literal["auto", "waypoints", "chain"]

JsonPayload = Union[List[List[str]], dict]
from urllib.parse import parse_qs, urlparse

_folium = None


def _require_folium():
    global _folium
    if _folium is not None:
        return _folium
    try:
        import folium as fl
    except ImportError:
        print("ERROR: folium is required. Run: pip install folium", file=sys.stderr)
        sys.exit(1)
    _folium = fl
    return _folium


# ---------------------------------------------------------------------------
# Colour palette (distinct, colourblind-friendly-ish)
# ---------------------------------------------------------------------------

ROUTE_COLORS = [
    "#e6194b",  # red
    "#3cb44b",  # green
    "#4363d8",  # blue
    "#f58231",  # orange
    "#911eb4",  # purple
    "#f032e6",  # magenta
    "#469990",  # teal
    "#9a6324",  # brown
    "#800000",  # dark red
    "#808000",  # olive
    "#000075",  # navy
    "#c0392b",  # tomato red
    "#27ae60",  # emerald
    "#2980b9",  # medium blue
    "#d35400",  # dark orange
    "#8e44ad",  # medium purple
    "#16a085",  # dark teal
    "#e67e22",  # golden orange
    "#2c3e50",  # slate
    "#6c3483",  # deep purple
    "#1a5276",  # steel blue
    "#117a65",  # dark emerald
    "#784212",  # dark brown
    "#943126",  # brick red
    "#1f618d",  # dark blue
    "#7d6608",  # dark gold
    "#5b2333",  # burgundy
    "#0e6655",  # forest teal
    "#6e2f7c",  # dark violet
    "#884ea0",  # medium violet
]

# Spiral jitter applied only to circle markers that share the same coordinate.
# Points with unique coordinates are never moved.
_MARKER_JITTER_DEG: float = 0.00001   # degrees latitude per step (~1.1 m)
_GOLDEN_ANGLE: float = 2.39996        # radians ≈ 137.5°, maximises angular spread


def _jitter_entries(
    entries: List[Tuple[int, Tuple[float, float]]],
) -> List[Tuple[int, Tuple[float, float]]]:
    """
    Apply jitter only to circle markers whose coordinate is shared by another marker.

    Unique coordinates are plotted at their exact position.
    Colliding coordinates are spread using a two-layer strategy:
    1. Per-route base offset: route ri is nudged ~0.6 m in direction ri×golden_angle,
       so markers from different routes landing at the same coord separate visually.
    2. Within-route spiral: if the same coord repeats within a route, each extra
       occurrence spirals out by _MARKER_JITTER_DEG per step (~1.1 m).

    Sequence labels (eye toggle) are built from the jittered positions via
    _build_seq_data, so they always appear directly above the correct circle.
    """
    # Count how many markers land on each coordinate (1 m resolution)
    coord_count: Counter = Counter(
        (round(lat, 5), round(lng, 5)) for _, (lat, lng) in entries
    )

    route_coord_count: dict = {}  # (ri, key) → times this coord seen in this route
    result: List[Tuple[int, Tuple[float, float]]] = []

    for ri, (lat, lng) in entries:
        key = (round(lat, 5), round(lng, 5))

        # Unique coord — no jitter, keep exact position
        if coord_count[key] == 1:
            result.append((ri, (lat, lng)))
            continue

        rkey = (ri, key)
        n = route_coord_count.get(rkey, 0)
        route_coord_count[rkey] = n + 1

        cos_lat = math.cos(math.radians(lat)) or 1.0

        # Base directional offset unique to this route
        base_angle = ri * _GOLDEN_ANGLE
        base = _MARKER_JITTER_DEG * 0.6
        dlat = base * math.cos(base_angle)
        dlng = base * math.sin(base_angle) / cos_lat

        # Extra spiral for within-route duplicate coords
        if n > 0:
            angle = base_angle + n * _GOLDEN_ANGLE
            step = _MARKER_JITTER_DEG * n
            dlat += step * math.cos(angle)
            dlng += step * math.sin(angle) / cos_lat

        result.append((ri, (round(lat + dlat, 6), round(lng + dlng, 6))))

    return result


def _build_seq_data(
    entries: List[Tuple[int, Tuple[float, float]]],
) -> dict:
    """
    Group already-jittered entries into route_index → [[lat, lng], ...] for the eye toggle.

    Call this after _jitter_entries so sequence labels land exactly on their circle markers.
    """
    seq_data: dict = {}
    for ri, (lat, lng) in entries:
        seq_data.setdefault(ri, []).append([lat, lng])
    return seq_data


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


def count_encoded_coordinate_tokens(nested_urls: List[List[str]]) -> int:
    """
    Count every coordinate occurrence in all URLs (no deduplication).

    Per direction URL: 1 origin + each waypoint (including consecutive
    duplicates) + 1 destination. A handoff point is counted twice when it
    appears as destination of one URL and origin of the next.
    """
    total = 0
    for route in nested_urls:
        for url in route:
            o, d, wps = extract_coordinates_from_url(url)
            total += (1 if o else 0) + len(wps) + (1 if d else 0)
    return total


def route_to_coords_waypoints_only(route_urls: List[str]) -> List[Tuple[float, float]]:
    """
    Only ``waypoints`` from each direction URL (no origin/destination).

    Matches typical **solver** exports: segment endpoints are routing-only;
    every delivery appears in the waypoint list. Sum over routes equals
    ``tokens - 2 * segments``.
    """
    coords: List[Tuple[float, float]] = []
    for url in route_urls:
        _origin, _destination, waypoints = extract_coordinates_from_url(url)
        coords.extend(w for w in waypoints if w)
    return coords


def route_to_coords_chain_merged(route_urls: List[str]) -> List[Tuple[float, float]]:
    """
    Full segment polyline (origin + waypoints + destination), merging the duplicate
    lat/lng at each URL boundary; strip leading depot and trailing return-to-depot.

    Typical **Amazon / multi-segment** exports: stops that sit only on a segment
    endpoint (not duplicated in ``waypoints``) are included. Total is
    ``waypoints_sum + (segments - routes)`` for a consistent file shape.
    """
    chain: List[Tuple[float, float]] = []
    for url in route_urls:
        origin, destination, waypoints = extract_coordinates_from_url(url)
        segment_points = [p for p in ([origin] + waypoints + [destination]) if p]
        for i, p in enumerate(segment_points):
            if i == 0 and chain and chain[-1] == p:
                continue
            chain.append(p)
    if not chain:
        return []
    delivery = chain[1:]
    if delivery and delivery[-1] == chain[0]:
        delivery = delivery[:-1]
    return delivery


def route_to_coords(route_urls: List[str], *, mode: CoordMode = "waypoints") -> List[Tuple[float, float]]:
    """Delivery stops for one route; ``mode`` selects counting semantics (see module doc)."""
    if mode == "waypoints":
        return route_to_coords_waypoints_only(route_urls)
    return route_to_coords_chain_merged(route_urls)


def delivery_coord_totals(nested_urls: List[List[str]]) -> Tuple[int, int]:
    """``(waypoints_only_total, chain_merged_total)`` for the whole file."""
    wp = sum(len(route_to_coords_waypoints_only(r)) for r in nested_urls)
    ch = sum(len(route_to_coords_chain_merged(r)) for r in nested_urls)
    return wp, ch


def resolve_delivery_coord_mode(
    nested_urls: List[List[str]],
    *,
    canonical: Optional[int],
    force: CoordModeSpec = "auto",
    doc_coord_mode: Optional[CoordMode] = None,
) -> CoordMode:
    """
    Choose how to turn URLs into an ordered delivery-stop list.

    * ``force`` ``waypoints`` / ``chain``: fixed mode (CLI ``--coord-mode``).
    * Else JSON ``coord_mode`` key (``waypoints`` / ``chain``) on the route object root.
    * Else if ``canonical`` (``delivery_stop_count``) equals one total, use that mode.
    * Else if ``canonical`` is set: pick the base whose total is closer (fewer padded markers).
    * Else default ``waypoints`` (solver-style).  **No filename/path guessing** — AMZ exports
      that need chain-merged counts must set ``delivery_stop_count`` (or ``coord_mode``) in JSON.
    """
    if force == "waypoints":
        return "waypoints"
    if force == "chain":
        return "chain"
    if doc_coord_mode is not None:
        return doc_coord_mode
    wp, ch = delivery_coord_totals(nested_urls)
    if canonical is not None:
        if canonical == wp:
            return "waypoints"
        if canonical == ch:
            return "chain"
        return "waypoints" if abs(canonical - wp) <= abs(canonical - ch) else "chain"
    return "waypoints"


def count_parsed_delivery_stops_from_nested(
    nested_urls: List[List[str]],
    *,
    mode: CoordMode = "waypoints",
) -> int:
    """Total delivery stops for all routes using the given coordinate mode."""
    return sum(len(route_to_coords(r, mode=mode)) for r in nested_urls)


@dataclass(frozen=True)
class DeliveryStopCountValidation:
    """Result of comparing parsed URL stops vs JSON ``delivery_stop_count``."""

    ok: bool
    parsed_total: int
    expected: int
    delta: int  # parsed_total - expected (meaningful when ok)
    routes_count: int
    waypoints_total: int
    chain_total: int
    matched_mode: Optional[CoordMode]


def validate_delivery_stop_count_match(
    nested_urls: List[List[str]],
    expected: int,
) -> DeliveryStopCountValidation:
    """
    OK if ``expected`` equals either waypoints-only or chain-merged totals
    (two common export conventions).
    """
    wp, ch = delivery_coord_totals(nested_urls)
    n = len(nested_urls)
    if expected == wp:
        return DeliveryStopCountValidation(
            ok=True,
            parsed_total=wp,
            expected=expected,
            delta=0,
            routes_count=n,
            waypoints_total=wp,
            chain_total=ch,
            matched_mode="waypoints",
        )
    if expected == ch:
        return DeliveryStopCountValidation(
            ok=True,
            parsed_total=ch,
            expected=expected,
            delta=0,
            routes_count=n,
            waypoints_total=wp,
            chain_total=ch,
            matched_mode="chain",
        )
    closer = wp if abs(expected - wp) <= abs(expected - ch) else ch
    return DeliveryStopCountValidation(
        ok=False,
        parsed_total=closer,
        expected=expected,
        delta=closer - expected,
        routes_count=n,
        waypoints_total=wp,
        chain_total=ch,
        matched_mode=None,
    )


def format_delivery_stop_validation(v: DeliveryStopCountValidation) -> str:
    """Single-block human-readable message for CLI / logs."""
    if v.ok:
        how = "waypoints-only" if v.matched_mode == "waypoints" else "chain-merged"
        return (
            f"Delivery stop count OK: {how} total {v.parsed_total:,} from {v.routes_count:,} route(s) "
            f"== delivery_stop_count {v.expected:,} "
            f"(other mode: {v.chain_total if v.matched_mode == 'waypoints' else v.waypoints_total:,})."
        )
    return (
        f"Delivery stop count MISMATCH: delivery_stop_count {v.expected:,} matches neither "
        f"waypoints-only {v.waypoints_total:,} nor chain-merged {v.chain_total:,} "
        f"(closest delta {v.delta:+,})."
    )


def route_depot_coord(route_urls: List[str]) -> Optional[Tuple[float, float]]:
    """Warehouse / start: ``origin`` of the first direction URL in the route."""
    if not route_urls:
        return None
    origin, _, _ = extract_coordinates_from_url(route_urls[0])
    return origin


def depot_entries_from_nested(
    nested_urls: List[List[str]],
) -> List[Tuple[int, Tuple[float, float]]]:
    """(route_index, depot_coord) for each route that has a first-segment origin."""
    out: List[Tuple[int, Tuple[float, float]]] = []
    for ri, route_urls in enumerate(nested_urls):
        d = route_depot_coord(route_urls)
        if d:
            out.append((ri, d))
    return out


# First match wins (solver exports use different names).
CANONICAL_STOP_COUNT_KEYS = (
    "delivery_stop_count",
    "optimized_waypoints_count",
    "stop_count",
    "total_delivery_stops",
)


def canonical_stop_count_from_dict(d: dict) -> Optional[int]:
    """Solver-reported delivery-stop total from a route JSON object root."""
    for key in CANONICAL_STOP_COUNT_KEYS:
        v = d.get(key)
        if v is not None:
            return int(v)
    return None


def load_routes_json_payload(
    raw: JsonPayload,
) -> Tuple[List[List[str]], Optional[int], Optional[CoordMode]]:
    """
    Accepts either a bare list of route URL lists, or an object:
      { "routes": [...], "delivery_stop_count": 8205 }
      { "routes": [...], "coord_mode": "chain" }  # optional explicit stop semantics

    Canonical count keys (see CANONICAL_STOP_COUNT_KEYS).
    Third return is ``coord_mode`` from JSON when set to ``waypoints`` or ``chain``.
    """
    if isinstance(raw, list):
        return raw, None, None
    if isinstance(raw, dict):
        routes = raw.get("routes")
        if routes is None:
            raise ValueError("JSON object must include a 'routes' key (list of route URL lists).")
        n = canonical_stop_count_from_dict(raw)
        cm = raw.get("coord_mode")
        doc_mode: Optional[CoordMode] = None
        if cm in ("waypoints", "chain"):
            doc_mode = cm  # type: ignore[assignment]
        return routes, n, doc_mode
    raise ValueError("JSON root must be a list or an object with 'routes'.")


def flatten_route_stop_entries(
    nested_urls: List[List[str]],
    *,
    coord_mode: CoordMode = "waypoints",
) -> List[Tuple[int, Tuple[float, float]]]:
    """(route_index, coord) in visit order across all routes."""
    out: List[Tuple[int, Tuple[float, float]]] = []
    for route_idx, route_urls in enumerate(nested_urls):
        for coord in route_to_coords(route_urls, mode=coord_mode):
            out.append((route_idx, coord))
    return out


def pad_stop_entries_to_count(
    entries: List[Tuple[int, Tuple[float, float]]],
    target: int,
) -> List[Tuple[int, Tuple[float, float]]]:
    """
    If len(entries) < target, insert extra markers by duplicating coordinates
    already present — first along runs of consecutive identical (lat,lng), then
    by appending copies of the last point. Same route_index as the duplicated stop.
    """
    if len(entries) >= target:
        return entries
    need = target - len(entries)
    result = list(entries)
    i = 0
    while need > 0 and i < len(result) - 1:
        ri, c0 = result[i]
        _, c1 = result[i + 1]
        if c0 == c1:
            result.insert(i + 1, (ri, c0))
            need -= 1
            i += 2
        else:
            i += 1
    while need > 0 and result:
        ri, c = result[-1]
        result.append((ri, c))
        need -= 1
    return result


def apply_delivery_stop_count_to_entries(
    entries: List[Tuple[int, Tuple[float, float]]],
    display_stop_count: Optional[int],
    *,
    context: str = "map",
) -> List[Tuple[int, Tuple[float, float]]]:
    """
    Pad (route_idx, coord) rows to display_stop_count when the solver has more
    logical stops than distinct URL coordinates. Same rules as generate_map.
    """
    if not entries or display_stop_count is None:
        return entries
    if display_stop_count < len(entries):
        print(
            f"  [WARN] delivery_stop_count={display_stop_count} < "
            f"parsed {len(entries)} positions ({context}); showing all parsed stops.",
            file=sys.stderr,
        )
        return entries
    before = len(entries)
    entries = pad_stop_entries_to_count(entries, display_stop_count)
    if len(entries) != display_stop_count:
        raise RuntimeError(
            f"Internal error ({context}): pad_stop_entries_to_count "
            f"target={display_stop_count} but len={len(entries)} (was {before})."
        )
    if len(entries) > before:
        print(
            f"  Padded {context} markers: {before} → {len(entries)} "
            f"(shared-coordinate stops duplicated for display).",
            file=sys.stderr,
        )
    return entries


def prepare_stop_entries(
    nested_urls: List[List[str]],
    display_stop_count: Optional[int] = None,
    *,
    coord_mode: CoordMode = "waypoints",
) -> List[Tuple[int, Tuple[float, float]]]:
    """Flatten URL coords to (route_idx, coord); optionally pad to match solver row count."""
    entries = flatten_route_stop_entries(nested_urls, coord_mode=coord_mode)
    return apply_delivery_stop_count_to_entries(entries, display_stop_count, context="map")


def add_stop_entries_to_map(
    layer: Any,
    entries: List[Tuple[int, Tuple[float, float]]],
    label_prefix: str = "",
) -> int:
    """Draw one CircleMarker per entry (identical coords → stacked markers)."""
    fl = _require_folium()
    if not entries:
        return 0
    totals = Counter(ri for ri, _ in entries)
    seen: Counter = Counter()
    for ri, (lat, lng) in entries:
        color = ROUTE_COLORS[ri % len(ROUTE_COLORS)]
        seen[ri] += 1
        pos = seen[ri]
        n = totals[ri]
        route_label = f"{label_prefix}Route {ri + 1}"
        tip = f"{route_label} · stop {pos}/{n}"
        popup_html = (
            f"<b>{route_label}</b><br>"
            f"Delivery stop {pos} of {n}<br>"
            f"{lat:.5f}, {lng:.5f}"
        )
        fl.CircleMarker(
            location=(lat, lng),
            radius=5,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            popup=fl.Popup(popup_html, max_width=220),
        ).add_to(layer)
    return len(entries)


# Depot map style: black stroke, light gray fill (same for every route).
DEPOT_MARKER_OUTLINE = "#000000"
DEPOT_MARKER_FILL = "#d4d4d4"


def add_depot_markers_to_map(
    layer: Any,
    depot_entries: List[Tuple[int, Tuple[float, float]]],
    *,
    distance_by_route: Optional[dict] = None,
    label_prefix: str = "",
) -> int:
    """
    One square marker per route depot (distinct from delivery CircleMarkers).

    ``depot_entries``: (route_index, (lat, lng)) in visit order; same depot lat/lng
    across routes produces stacked markers.

    Click popup: only the word Depot and coordinates. Outline black, fill light gray.
    ``distance_by_route`` is ignored (kept for call-site compatibility).
    """
    fl = _require_folium()
    if not depot_entries:
        return 0
    _ = distance_by_route  # reserved; popup stays minimal
    for _ri, (lat, lng) in depot_entries:
        popup_html = f"Depot<br>{lat:.5f}, {lng:.5f}"
        fl.RegularPolygonMarker(
            location=(lat, lng),
            number_of_sides=4,
            radius=9,
            color=DEPOT_MARKER_OUTLINE,
            weight=2,
            fill=True,
            fill_color=DEPOT_MARKER_FILL,
            fill_opacity=1.0,
            rotation=45,
            tooltip="Depot",
            popup=fl.Popup(popup_html, max_width=200),
        ).add_to(layer)
    return len(depot_entries)


# ---------------------------------------------------------------------------
# Map building
# ---------------------------------------------------------------------------

def centroid(all_coords: List[Tuple[float, float]]) -> Tuple[float, float]:
    lats = [c[0] for c in all_coords]
    lngs = [c[1] for c in all_coords]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


def add_routes_to_map(
    layer: Any,
    nested_urls: List[List[str]],
    label_prefix: str = "",
    display_stop_count: Optional[int] = None,
    *,
    coord_mode: CoordMode = "waypoints",
) -> int:
    """See prepare_stop_entries + add_stop_entries_to_map."""
    entries = prepare_stop_entries(
        nested_urls, display_stop_count, coord_mode=coord_mode
    )
    return add_stop_entries_to_map(layer, entries, label_prefix)


def build_single_map(
    nested_urls: List[List[str]],
    title: str = "Routes",
    description: str = "",
    display_stop_count: Optional[int] = None,
    *,
    coord_mode: CoordMode = "waypoints",
    panel_report_total_stops: Optional[int] = None,
) -> Tuple[Any, int]:
    """Builds a map for a single dataset. Returns (map, marker_count)."""
    fl = _require_folium()
    entries = prepare_stop_entries(
        nested_urls, display_stop_count, coord_mode=coord_mode
    )
    if not entries:
        raise ValueError("No valid coordinates found in input.")

    depot_entries = depot_entries_from_nested(nested_urls)
    center_pts = [c for _, c in entries] + [c for _, c in depot_entries]
    center = centroid(center_pts)
    fmap = fl.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    # Top-center title + description banner (with X to close)
    desc_line = (
        f'<div style="font-size:11px;color:#555;margin-top:4px;max-width:560px">{description}</div>'
        if description else ""
    )
    banner_html = f"""
    <div id="map-banner" style="position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:9999">
        <div style="position:relative;background:white;padding:8px 36px 8px 20px;border-radius:6px;
                    border:1px solid #ccc;font-family:sans-serif;
                    box-shadow:2px 2px 6px rgba(0,0,0,.2);text-align:center">
            <div style="font-size:14px;font-weight:bold">{title}</div>
            {desc_line}
            <span onclick="document.getElementById('map-banner').style.display='none'"
                  style="position:absolute;top:4px;right:10px;cursor:pointer;font-size:18px;
                         color:#bbb;line-height:1" title="Close">&#215;</span>
        </div>
    </div>
    """
    fmap.get_root().html.add_child(fl.Element(banner_html))

    # Apply coordinate jitter so overlapping dots from different routes separate visually
    entries = _jitter_entries(entries)

    marker_total = len(entries)
    headline_stops = (
        panel_report_total_stops
        if panel_report_total_stops is not None
        else marker_total
    )
    stops_per_route: Counter = Counter(ri for ri, _ in entries)

    # Build stop sequence data for the eye toggle (positions already jittered)
    seq_json = json.dumps(_build_seq_data(entries), separators=(",", ":"))

    extra_stop_row = ""
    if headline_stops != marker_total:
        extra_stop_row = (
            f"<tr style='font-size:10px;color:#888'>"
            f"<td style='padding:2px 8px 2px 0'>Drawn markers</td>"
            f"<td style='text-align:right;padding:2px 0'><b>{marker_total:,}</b></td></tr>"
        )

    _eye_svg = (
        "<svg width='16' height='11' viewBox='0 0 16 11' fill='none'"
        " stroke='currentColor' stroke-width='1.4' stroke-linecap='round'>"
        "<path d='M1 5.5C3 2 5.5 1 8 1s5 1 7 4.5C13 9 10.5 10 8 10S3 9 1 5.5Z'/>"
        "<circle cx='8' cy='5.5' r='2'/></svg>"
    )

    # Per-route rows with eye toggle for stop sequence numbers (eye on left)
    route_rows_html = "".join(
        f"<tr>"
        f"<td style='padding:1px 4px 1px 0'>"
        f"<span id='eye-{ri}' class='eye-btn' onclick='toggleSeq({ri})' title='Show stop sequence'>{_eye_svg}</span>"
        f"</td>"
        f"<td style='padding:1px 6px 1px 0;color:{ROUTE_COLORS[ri % len(ROUTE_COLORS)]}'>"
        f"&#9679;</td>"
        f"<td style='padding:1px 6px 1px 0'>Route {ri + 1}</td>"
        f"<td style='padding:1px 0;text-align:right'>{cnt:,} stops</td>"
        f"</tr>"
        for ri, cnt in sorted(stops_per_route.items())
    )

    panel_html = f"""
    <div id="summary-panel" style="position:fixed;top:16px;right:16px;background:white;
                padding:12px 16px;border-radius:8px;border:1px solid #ccc;
                font-family:monospace;font-size:13px;z-index:9999;
                box-shadow:2px 2px 8px rgba(0,0,0,.2);min-width:200px;max-width:290px">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <b>{title}</b>
            <span onclick="
                var t=document.getElementById('summary-body-a');
                t.style.display=t.style.display==='none'?'block':'none';
                this.textContent=t.style.display==='none'?'▶':'▼';
            " style="cursor:pointer;font-size:11px;margin-left:10px">▼</span>
        </div>
        <div id="summary-body-a">
            <hr style="margin:6px 0">
            <table style="border-collapse:collapse;width:100%;font-size:11px;color:#666;line-height:1.5">
                <tr><td style="padding:2px 8px 2px 0">Total routes</td>
                    <td style="text-align:right;padding:2px 0"><b>{len(nested_urls):,}</b></td></tr>
                <tr><td style="padding:2px 8px 2px 0">Total stops</td>
                    <td style="text-align:right;padding:2px 0"><b>{headline_stops:,}</b></td></tr>{extra_stop_row}
            </table>
            <hr style="margin:6px 0">
            <div style="max-height:55vh;overflow-y:auto">
                <table style="border-collapse:collapse;width:100%;font-size:12px">
                    {route_rows_html}
                </table>
            </div>
        </div>
    </div>
    """
    fmap.get_root().html.add_child(fl.Element(panel_html))
    fmap.get_root().html.add_child(fl.Element(
        "<style>"
        ".seq-lbl{background:rgba(255,255,255,.92);border:1px solid #555;border-radius:3px;"
        "font:11px/1.3 monospace;padding:0 2px;color:#222;display:inline-block}"
        ".eye-btn{cursor:pointer;opacity:.6;color:#999;user-select:none;transition:opacity .15s,color .15s;vertical-align:middle}"
        ".eye-btn:hover{opacity:1;color:#2980b9}"
        ".eye-btn.on{opacity:1;color:#2980b9}"
        "</style>"
        "<script>"
        "var _routeSeq=" + seq_json + ";"
        "var _seqLayers={};"
        "var _lmap=null;"
        "function _getMap(){if(_lmap)return _lmap;"
        "for(var k in window){try{if(window[k]&&window[k]._leaflet_id!==undefined&&window[k].getCenter){_lmap=window[k];break;}}catch(e){}}"
        "return _lmap;}"
        "function toggleSeq(ri){"
        "var btn=document.getElementById('eye-'+ri);"
        "if(_seqLayers[ri]){_seqLayers[ri].forEach(function(m){m.remove();});delete _seqLayers[ri];if(btn)btn.classList.remove('on');}"
        "else{var pts=_routeSeq[ri];if(!pts)return;var lm=_getMap();if(!lm)return;"
        "_seqLayers[ri]=pts.map(function(pt,i){"
        "return L.marker([pt[0],pt[1]],"
        "{icon:L.divIcon({className:'',html:'<span class=\"seq-lbl\">'+(i+1)+'</span>',iconSize:null,iconAnchor:[7,14]}),"
        "interactive:false,zIndexOffset:1000}).addTo(lm);});"
        "if(btn)btn.classList.add('on');"
        "lm.fitBounds(L.latLngBounds(pts),{padding:[60,60],maxZoom:16});}}"
        "</script>"
    ))

    layer = fl.FeatureGroup(name="Stops").add_to(fmap)
    add_stop_entries_to_map(layer, entries)
    depot_layer = fl.FeatureGroup(name="Depot").add_to(fmap)
    add_depot_markers_to_map(depot_layer, depot_entries)
    return fmap, marker_total


def build_comparison_map(
    urls_a: List[List[str]],
    urls_b: List[List[str]],
    label_a: str = "Dataset A",
    label_b: str = "Dataset B",
    description: str = "",
    *,
    coord_mode_a: CoordMode = "waypoints",
    coord_mode_b: CoordMode = "waypoints",
) -> Any:
    """Builds a side-by-side comparison map with layer toggle."""
    fl = _require_folium()
    all_coords: List[Tuple[float, float]] = []
    for route_urls in urls_a:
        all_coords.extend(route_to_coords(route_urls, mode=coord_mode_a))
    for route_urls in urls_b:
        all_coords.extend(route_to_coords(route_urls, mode=coord_mode_b))
    for nested in (urls_a, urls_b):
        for _, c in depot_entries_from_nested(nested):
            all_coords.append(c)

    if not all_coords:
        raise ValueError("No valid coordinates found in inputs.")

    center = centroid(all_coords)
    fmap = fl.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    def total_stops(nested: List[List[str]], mode: CoordMode) -> int:
        return sum(len(route_to_coords(r, mode=mode)) for r in nested)

    # Top-center title + description banner (with X to close)
    desc_line = (
        f'<div style="font-size:11px;color:#555;margin-top:4px;max-width:600px">{description}</div>'
        if description else ""
    )
    title_html = f"""
    <div id="map-banner" style="position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:9999">
        <div style="position:relative;background:white;padding:8px 36px 8px 20px;border-radius:6px;
                    border:1px solid #ccc;font-family:sans-serif;
                    box-shadow:2px 2px 6px rgba(0,0,0,.2);text-align:center">
            <div style="font-size:14px;font-weight:bold">Comparison: {label_a} vs {label_b}</div>
            {desc_line}
            <span onclick="document.getElementById('map-banner').style.display='none'"
                  style="position:absolute;top:4px;right:10px;cursor:pointer;font-size:18px;
                         color:#bbb;line-height:1" title="Close">&#215;</span>
        </div>
    </div>
    """
    fmap.get_root().html.add_child(fl.Element(title_html))

    group_a = fl.FeatureGroup(
        name=f"{label_a} ({len(urls_a)} routes, {total_stops(urls_a, coord_mode_a)} stops)",
        show=True,
    )
    group_b = fl.FeatureGroup(
        name=f"{label_b} ({len(urls_b)} routes, {total_stops(urls_b, coord_mode_b)} stops)",
        show=True,
    )

    add_routes_to_map(
        group_a, urls_a, label_prefix=f"[{label_a}] ", coord_mode=coord_mode_a
    )
    add_depot_markers_to_map(
        group_a, depot_entries_from_nested(urls_a), label_prefix=f"[{label_a}] "
    )
    add_routes_to_map(
        group_b, urls_b, label_prefix=f"[{label_b}] ", coord_mode=coord_mode_b
    )
    add_depot_markers_to_map(
        group_b, depot_entries_from_nested(urls_b), label_prefix=f"[{label_b}] "
    )

    group_a.add_to(fmap)
    group_b.add_to(fmap)
    fl.LayerControl(collapsed=False).add_to(fmap)

    return fmap


# ---------------------------------------------------------------------------
# Map from pre-computed OSRM results (called by check_distance_osrm.py)
# ---------------------------------------------------------------------------

def build_map_from_route_data(
    route_data_list,        # List[RouteData] from check_distance_osrm
    total_km: float,
    label: str = "Routes",
    description: str = "",
    output_path: str = "map.html",
    display_stop_count: Optional[int] = None,
    panel_report_total_stops: Optional[int] = None,
) -> None:
    """
    Builds an HTML map using route coords and distances already computed by OSRM.

    Circle markers only (no polylines). If display_stop_count is set (e.g. from
    JSON delivery_stop_count), pads markers like generate_map.py so the map
    matches sum(len(optimized_waypoints)).

    The panel **Total stops** headline uses ``panel_report_total_stops`` when set
    (e.g. JSON ``delivery_stop_count`` or ``--delivery-stops``); otherwise the
    marker count. If that canonical value differs from the number of circle
    markers drawn, an extra row shows the marker total and a short note.

    **Marker count guarantee:** if ``display_stop_count`` is set and
    ``display_stop_count >=`` the number of parsed URL positions, the map draws
    exactly ``display_stop_count`` delivery circle markers (padding duplicates
    coordinates as needed). A logic bug in padding raises ``RuntimeError``.

    If ``display_stop_count`` is missing, only parsed positions are drawn (no
    guarantee of matching an external solver total). If it is strictly smaller
    than parsed, all parsed markers are kept and counts cannot match metadata.

    Called by check_distance_osrm.py when --map is passed; no extra OSRM queries.
    """
    fl = _require_folium()
    entries: List[Tuple[int, Tuple[float, float]]] = []
    distance_by_route: dict = {}
    for r in route_data_list:
        distance_by_route[r.route_index] = r.distance_km
        for c in r.coords:
            entries.append((r.route_index, c))

    entries = apply_delivery_stop_count_to_entries(
        entries, display_stop_count, context="OSRM map"
    )
    if not entries:
        print("  [WARN] No coordinates to map.")
        return

    # Apply coordinate jitter so overlapping dots from different routes separate visually
    entries = _jitter_entries(entries)

    marker_total = len(entries)
    headline_total = (
        panel_report_total_stops
        if panel_report_total_stops is not None
        else marker_total
    )

    depot_entries = [
        (r.route_index, r.depot)
        for r in route_data_list
        if r.depot is not None
    ]
    center = centroid([c for _, c in entries] + [c for _, c in depot_entries])
    fmap = fl.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    # Top-center title + description banner (with X to close)
    desc_line = (
        f'<div style="font-size:11px;color:#555;margin-top:4px;max-width:560px">{description}</div>'
        if description else ""
    )
    banner_html = f"""
    <div id="map-banner" style="position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:9999">
        <div style="position:relative;background:white;padding:8px 36px 8px 20px;border-radius:6px;
                    border:1px solid #ccc;font-family:sans-serif;
                    box-shadow:2px 2px 6px rgba(0,0,0,.2);text-align:center">
            <div style="font-size:14px;font-weight:bold">{label}</div>
            {desc_line}
            <span onclick="document.getElementById('map-banner').style.display='none'"
                  style="position:absolute;top:4px;right:10px;cursor:pointer;font-size:18px;
                         color:#bbb;line-height:1" title="Close">&#215;</span>
        </div>
    </div>
    """
    fmap.get_root().html.add_child(fl.Element(banner_html))

    # Build stop sequence data for the eye toggle (positions already jittered)
    seq_json = json.dumps(_build_seq_data(entries), separators=(",", ":"))

    layer = fl.FeatureGroup(name="Stops").add_to(fmap)

    totals = Counter(ri for ri, _ in entries)
    seen: Counter = Counter()
    for ri, (lat, lng) in entries:  # lat/lng are already jittered
        color = ROUTE_COLORS[ri % len(ROUTE_COLORS)]
        seen[ri] += 1
        pos = seen[ri]
        n = totals[ri]
        route_km = distance_by_route.get(ri, 0.0)
        popup_html = (
            f"<b>Route {ri + 1}</b><br>"
            f"Delivery stop {pos} of {n}<br>"
            f"Route distance (OSRM): {route_km:.2f} km<br>"
            f"{lat:.5f}, {lng:.5f}"
        )
        fl.CircleMarker(
            location=(lat, lng),
            radius=5,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            popup=fl.Popup(popup_html, max_width=240),
        ).add_to(layer)

    depot_layer = fl.FeatureGroup(name="Depot").add_to(fmap)
    add_depot_markers_to_map(
        depot_layer,
        depot_entries,
        distance_by_route=distance_by_route,
    )

    # Build per-route stop counts from the (possibly padded) entries
    stops_per_route: Counter = Counter(ri for ri, _ in entries)
    assert sum(stops_per_route.values()) == marker_total

    _eye_svg = (
        "<svg width='16' height='11' viewBox='0 0 16 11' fill='none'"
        " stroke='currentColor' stroke-width='1.4' stroke-linecap='round'>"
        "<path d='M1 5.5C3 2 5.5 1 8 1s5 1 7 4.5C13 9 10.5 10 8 10S3 9 1 5.5Z'/>"
        "<circle cx='8' cy='5.5' r='2'/></svg>"
    )

    # Route rows for the summary table (with eye toggle for sequence numbers, eye on left)
    route_rows_html = "".join(
        f"<tr>"
        f"<td style='padding:1px 4px 1px 0'>"
        f"<span id='eye-{r.route_index}' class='eye-btn' onclick='toggleSeq({r.route_index})' title='Show stop sequence'>{_eye_svg}</span>"
        f"</td>"
        f"<td style='padding:1px 6px 1px 0;color:{ROUTE_COLORS[r.route_index % len(ROUTE_COLORS)]}'>"
        f"&#9679;</td>"
        f"<td style='padding:1px 6px 1px 0'>Route {r.route_index + 1}</td>"
        f"<td style='padding:1px 6px 1px 0;text-align:right'>{stops_per_route[r.route_index]:,} stops</td>"
        f"<td style='padding:1px 0;text-align:right'>{r.distance_km:,.2f} km</td>"
        f"</tr>"
        for r in route_data_list
    )

    markers_row = ""
    reconcile_note = ""
    if headline_total != marker_total:
        markers_row = (
            f"""
                <tr style="color:#888;font-size:10px">
                    <td title="Circle markers on map; per-route rows sum to this">Drawn markers</td>
                    <td style="text-align:right"><b>{marker_total:,}</b></td>
                </tr>"""
        )
        delta = marker_total - headline_total
        reconcile_note = (
            f'<p style="font-size:10px;color:#777;margin:6px 0 0 0;line-height:1.35">'
            f'&quot;Total stops&quot; is delivery_stop_count / --delivery-stops. '
            f'Drawn markers differ by {delta:+,} (URLs vs metadata).</p>'
        )

    n_routes_panel = len(route_data_list)
    if n_routes_panel > 0:
        avg_stops_per_route = headline_total / n_routes_panel
        avg_km_per_route = total_km / n_routes_panel
    else:
        avg_stops_per_route = 0.0
        avg_km_per_route = 0.0

    panel_html = f"""
    <div id="summary-panel" style="position:fixed;top:16px;right:16px;background:white;
                padding:12px 16px;border-radius:8px;border:1px solid #ccc;
                font-family:monospace;font-size:13px;z-index:9999;
                box-shadow:2px 2px 8px rgba(0,0,0,.2);min-width:220px;max-width:300px">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <b>{label}</b>
            <span onclick="
                var t=document.getElementById('summary-body');
                t.style.display=t.style.display==='none'?'block':'none';
                this.textContent=t.style.display==='none'?'▶':'▼';
            " style="cursor:pointer;font-size:11px;margin-left:10px">▼</span>
        </div>
        <div id="summary-body">
            <hr style="margin:6px 0">
            <table style="border-collapse:collapse;width:100%;line-height:1.5">
                <tr style="color:#666;font-size:11px">
                    <td style="padding:2px 8px 2px 0;vertical-align:top">Total routes</td>
                    <td style="text-align:right;padding:2px 0;vertical-align:top"><b>{len(route_data_list):,}</b></td>
                </tr>
                <tr style="color:#666;font-size:11px">
                    <td style="padding:2px 8px 2px 0;vertical-align:top">Total stops</td>
                    <td style="text-align:right;padding:2px 0;vertical-align:top"><b>{headline_total:,}</b></td>
                </tr>
                <tr style="color:#666;font-size:11px">
                    <td style="padding:2px 8px 2px 0;vertical-align:top">Total km</td>
                    <td style="text-align:right;padding:2px 0;vertical-align:top"><b>{total_km:,.2f}</b> km</td>
                </tr>
                <tr style="color:#666;font-size:11px">
                    <td style="padding:2px 8px 2px 0;vertical-align:top">Stops / route</td>
                    <td style="text-align:right;padding:2px 0;vertical-align:top"><b>{avg_stops_per_route:,.1f}</b></td>
                </tr>
                <tr style="color:#666;font-size:11px">
                    <td style="padding:2px 8px 2px 0;vertical-align:top">Km / route</td>
                    <td style="text-align:right;padding:2px 0;vertical-align:top"><b>{avg_km_per_route:,.2f}</b> km</td>
                </tr>{markers_row}
            </table>
            {reconcile_note}
            <hr style="margin:6px 0">
            <div style="max-height:55vh;overflow-y:auto">
                <table style="border-collapse:collapse;width:100%;font-size:12px">
                    {route_rows_html}
                </table>
            </div>
        </div>
    </div>
    """
    fmap.get_root().html.add_child(fl.Element(panel_html))
    fmap.get_root().html.add_child(fl.Element(
        "<style>"
        ".seq-lbl{background:rgba(255,255,255,.92);border:1px solid #555;border-radius:3px;"
        "font:11px/1.3 monospace;padding:0 2px;color:#222;display:inline-block}"
        ".eye-btn{cursor:pointer;opacity:.6;color:#999;user-select:none;transition:opacity .15s,color .15s;vertical-align:middle}"
        ".eye-btn:hover{opacity:1;color:#2980b9}"
        ".eye-btn.on{opacity:1;color:#2980b9}"
        "</style>"
        "<script>"
        "var _routeSeq=" + seq_json + ";"
        "var _seqLayers={};"
        "var _lmap=null;"
        "function _getMap(){if(_lmap)return _lmap;"
        "for(var k in window){try{if(window[k]&&window[k]._leaflet_id!==undefined&&window[k].getCenter){_lmap=window[k];break;}}catch(e){}}"
        "return _lmap;}"
        "function toggleSeq(ri){"
        "var btn=document.getElementById('eye-'+ri);"
        "if(_seqLayers[ri]){_seqLayers[ri].forEach(function(m){m.remove();});delete _seqLayers[ri];if(btn)btn.classList.remove('on');}"
        "else{var pts=_routeSeq[ri];if(!pts)return;var lm=_getMap();if(!lm)return;"
        "_seqLayers[ri]=pts.map(function(pt,i){"
        "return L.marker([pt[0],pt[1]],"
        "{icon:L.divIcon({className:'',html:'<span class=\"seq-lbl\">'+(i+1)+'</span>',iconSize:null,iconAnchor:[7,14]}),"
        "interactive:false,zIndexOffset:1000}).addTo(lm);});"
        "if(btn)btn.classList.add('on');"
        "lm.fitBounds(L.latLngBounds(pts),{padding:[60,60],maxZoom:16});}}"
        "</script>"
    ))
    fmap.save(output_path)


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
    parser.add_argument("--description", default="", help="Optional subtitle shown in the map header banner")
    parser.add_argument(
        "--delivery-stops",
        type=int,
        default=None,
        metavar="N",
        help="Draw N delivery markers (e.g. sum(len(optimized_waypoints))). "
        "If N exceeds coords parsed from URLs, duplicate markers at shared lat/lng. "
        "Overrides delivery_stop_count / optimized_waypoints_count / stop_count in JSON.",
    )
    parser.add_argument(
        "--coord-mode",
        choices=("auto", "waypoints", "chain"),
        default="auto",
        help="Stop counting: waypoints-only, chain-merged, or auto "
        "(JSON coord_mode / delivery_stop_count match, else default waypoints).",
    )
    args = parser.parse_args()

    with open(args.input) as f:
        urls_a, file_stop_count, doc_mode_a = load_routes_json_payload(json.load(f))

    n_tokens = count_encoded_coordinate_tokens(urls_a)
    print(f"JSON coordinate tokens (incl. duplicates): {n_tokens:,}")

    urls_b = None
    file_stop_count_b: Optional[int] = None
    if args.compare:
        with open(args.compare) as f:
            urls_b, file_stop_count_b, doc_mode_b = load_routes_json_payload(json.load(f))
        n_tokens_b = count_encoded_coordinate_tokens(urls_b)
        print(f"JSON coordinate tokens [compare, incl. duplicates]: {n_tokens_b:,}")

    display_n = args.delivery_stops if args.delivery_stops is not None else file_stop_count
    canonical_resolve = (
        args.delivery_stops if args.delivery_stops is not None else file_stop_count
    )
    mode_a = resolve_delivery_coord_mode(
        urls_a,
        canonical=canonical_resolve,
        force=args.coord_mode,  # type: ignore[arg-type]
        doc_coord_mode=doc_mode_a,
    )
    print(
        f"Coord mode [{args.label_a}]: {mode_a} "
        f"(waypoints {delivery_coord_totals(urls_a)[0]:,} | chain {delivery_coord_totals(urls_a)[1]:,})"
    )

    if args.compare:
        assert urls_b is not None
        mode_b = resolve_delivery_coord_mode(
            urls_b,
            canonical=file_stop_count_b,
            force=args.coord_mode,  # type: ignore[arg-type]
            doc_coord_mode=doc_mode_b,
        )
        print(
            f"Coord mode [{args.label_b}]: {mode_b} "
            f"(waypoints {delivery_coord_totals(urls_b)[0]:,} | chain {delivery_coord_totals(urls_b)[1]:,})"
        )
        fmap = build_comparison_map(
            urls_a,
            urls_b,
            label_a=args.label_a,
            label_b=args.label_b,
            description=args.description,
            coord_mode_a=mode_a,
            coord_mode_b=mode_b,
        )
        pa = count_parsed_delivery_stops_from_nested(urls_a, mode=mode_a)
        pb = count_parsed_delivery_stops_from_nested(urls_b, mode=mode_b)
        print(f"Comparison map: {len(urls_a)} routes ({pa} pts) vs {len(urls_b)} routes ({pb} pts)")
    else:
        panel_total = (
            args.delivery_stops
            if args.delivery_stops is not None
            else file_stop_count
        )
        fmap, n_markers = build_single_map(
            urls_a,
            title=args.label_a,
            description=args.description,
            display_stop_count=display_n,
            coord_mode=mode_a,
            panel_report_total_stops=panel_total,
        )
        print(f"Map: {len(urls_a)} routes, {n_markers} markers")

    fmap.save(args.output)
    print(f"Saved → {args.output}")
