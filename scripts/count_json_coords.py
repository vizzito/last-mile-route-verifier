#!/usr/bin/env python3
"""
Count every coordinate in a route JSON file (Google Maps direction URLs).

``count_encoded_coordinate_tokens``: sum per URL of origin + waypoints + destination.
Origins and destinations are routing anchors (depot / segment-boundary handoffs),
not delivery stops.

Two delivery totals (see generate_map): **waypoints-only** and **chain-merged**.
Auto uses JSON ``coord_mode`` or ``delivery_stop_count`` match; else defaults to waypoints.

Does not require folium (only imports generate_map helpers that parse URLs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from generate_map import (  # noqa: E402
    count_encoded_coordinate_tokens,
    count_parsed_delivery_stops_from_nested,
    delivery_coord_totals,
    extract_coordinates_from_url,
    load_routes_json_payload,
    resolve_delivery_coord_mode,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Count coordinate tokens in route JSON (incl. duplicates)."
    )
    parser.add_argument("--input", required=True, help="Route JSON file")
    parser.add_argument(
        "--per-route",
        action="store_true",
        help="Print a line per route (still includes duplicates).",
    )
    args = parser.parse_args()

    with open(args.input) as f:
        routes, meta_count, doc_coord_mode = load_routes_json_payload(json.load(f))

    total_tokens = count_encoded_coordinate_tokens(routes)
    num_segments = sum(len(r) for r in routes)
    wp_total, chain_total = delivery_coord_totals(routes)
    mode = resolve_delivery_coord_mode(
        routes,
        canonical=meta_count,
        force="auto",
        doc_coord_mode=doc_coord_mode,
    )
    auto_total = count_parsed_delivery_stops_from_nested(routes, mode=mode)

    print(f"Coordinate tokens (origin+waypoints+dest): {total_tokens:,}")
    print(f"  Routing anchors (2 × {num_segments:,} segments):   {2 * num_segments:,}")
    print(f"  Delivery coords (waypoints-only):        {wp_total:,}")
    print(f"  Delivery coords (chain-merged):          {chain_total:,}")
    print(f"  Auto mode for this path/metadata:        {mode} → {auto_total:,}")
    if meta_count is not None:
        print(f"Canonical stop count in JSON:              {meta_count:,}")
        if meta_count not in (wp_total, chain_total):
            print(
                f"  → Matches neither waypoints nor chain "
                f"(Δ wp {meta_count - wp_total:+,}, Δ chain {meta_count - chain_total:+,})"
            )

    if args.per_route:
        print("Per route:")
        for i, route in enumerate(routes):
            rsum = 0
            for url in route:
                o, d, wps = extract_coordinates_from_url(url)
                rsum += (1 if o else 0) + len(wps) + (1 if d else 0)
            print(f"  route {i + 1}: {rsum:,}")


if __name__ == "__main__":
    main()
