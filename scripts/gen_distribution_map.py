#!/usr/bin/env python3
"""
Baidu Map Distribution Map Generator (v2 — uses shared utils)
===============================================================
Generate geographic distribution maps with numbered markers from address lists.

Usage:
    python3 scripts/gen_distribution_map.py --input data.xlsx --ak YOUR_AK --output map.png
    python3 scripts/gen_distribution_map.py --input data.json --ak YOUR_AK --output map.png

Author: YUI (OpenClaw Agent)
Date: 2026-04-09
"""

import argparse
import math
import os
import re
import sys
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Use shared utilities
from _shared_utils import (
    load_ak, batch_geocode, calibrate_pixel_scale,
    read_input_data, DEFAULT_WIDTH, DEFAULT_HEIGHT,
    STATIC_MAP_URL,
)


# ============================================================
# Render Distribution Map
# ============================================================

def render_distribution_map(results, output_path, ak,
                             zoom=None, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                             title=None, font_path=None):
    """Generate distribution map with numbered markers on calibrated coordinates."""
    points = [r for r in results if r.get("geo_status") == "OK"]
    n_total = len(results)
    n_ok = len(points)
    n_fail = n_total - n_ok

    if n_ok == 0:
        raise ValueError("No valid coordinates to plot!")

    lngs = [p["lng"] for p in points]
    lats = [p["lat"] for p in points]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)

    # Auto-zoom
    if zoom is None:
        avg_lat = sum(lats) / len(lats)
        km_lng = (max_lng - min_lng) * 111.32 * math.cos(math.radians(avg_lat))
        km_lat = (max_lat - min_lat) * 111.32
        max_km = max(km_lng, km_lat)
        zoom_table = [(3, 15), (8, 14), (16, 13), (35, 12), (70, 11)]
        zoom = next((z for km, z in zoom_table if max_km <= km), 10)

    center_lng = (min_lng + max_lng) / 2
    center_lat = (min_lat + max_lat) / 2
    w, h = min(width, 1024), min(height, 1024)

    print(f"\nMap: center=({center_lng:.4f},{center_lat:.4f}) zoom={zoom} {w}x{h}")
    print(f"Data: {n_ok} points, lng[{min_lng:.2f},{max_lng:.2f}] lat[{min_lat:.2f},{max_lat:.2f}]")

    # Calibrate
    ppd_x, ppd_y = calibrate_pixel_scale(center_lng, center_lat, zoom, w, h, ak)

    def to_pixel(lng, lat):
        return int(round(w / 2 + (lng - center_lng) * ppd_x)), \
               int(round(h / 2 - (lat - center_lat) * ppd_y))

    # Base map with native markers
    marker_str = "|".join(f"{p['lng']},{p['lat']}" for p in points)
    url_map = f"{STATIC_MAP_URL}?ak={ak}&center={center_lng},{center_lat}&width={w}&height={h}&zoom={zoom}&markers={marker_str}"
    print("Downloading base map with markers...")
    r_map = requests_get(url_map, timeout=30)

    if "image" not in r_map.headers.get("Content-Type", ""):
        raise RuntimeError(f"Baidu API error: {r_map.text[:300]}")

    map_img = Image.open(BytesIO(r_map.content)).convert("RGB")
    draw = ImageDraw.Draw(map_img)

    # Fonts
    try:
        fp = font_path or "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
        font_num = ImageFont.truetype(fp, 9)
        font_title = ImageFont.truetype(fp, 16)
        font_small = ImageFont.truetype(fp, 10)
    except OSError:
        font_num = font_title = font_small = ImageFont.load_default()

    # Numbered labels at calibrated positions
    count_visible = 0
    title_h = 42; bottom_h = 25; margin = 12
    for p in results:
        if p.get("geo_status") != "OK":
            continue
        idx = results.index(p) + 1
        px, py = to_pixel(p["lng"], p["lat"])
        if not (margin <= px <= w - margin and title_h <= py <= h - bottom_h):
            continue
        count_visible += 1
        label = str(idx)
        bbox = draw.textbbox((0, 0), label, font=font_num)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lx, ly = px + 3, py - th - 1
        draw.rectangle([lx - 1, ly - 1, lx + tw + 3, ly + th + 2], fill="#E53935")
        draw.text((lx, ly), label, fill="white", font=font_num)

    print(f"Labeled: {count_visible}/{n_ok} visible on map")

    # Title bar
    overlay = Image.new("RGBA", (w, title_h), (20, 20, 20, 220))
    map_img.paste(overlay.crop((0, 0, w, title_h)), (0, 0), overlay)
    draw = ImageDraw.Draw(map_img)
    draw.text((12, 11), title or f"分布地图 | 共{n_total}家 成功定位{n_ok}家",
              fill="white", font=font_title)

    # Bottom stats
    districts = {}
    for p in points:
        addr = p.get("geo_address", "")
        m = re.search(r"广州市([^市]+?)(?:区|县|市)", addr)
        d = m.group(1) if m else "未知"
        districts[d] = districts.get(d, 0) + 1
    stat_parts = [f"{d}{c}家" for d, c in sorted(districts.items(), key=lambda x: -x[1])]
    stats_text = "  ".join(stat_parts[:7])
    if len(stat_parts) > 7:
        stats_text += f" 等{len(stat_parts)}个区域"
    draw.text((12, h - 18), f"分布: {stats_text}", fill="#333333", font=font_small)

    # Save
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    map_img.save(output_path, quality=92)
    size_kb = os.path.getsize(output_path) // 1024
    print(f"\n✅ Saved: {output_path} ({size_kb}KB)")
    return output_path


def requests_get(url, **kw):
    """Lazy import to avoid dependency at module level."""
    import requests as _req
    return _req.get(url, **kw)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate geographic distribution map from address list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/gen_distribution_map.py --input customers.xlsx --ak YOUR_KEY --output map.png
  python3 scripts/gen_distribution_map.py --input data.json --ak YOUR_KEY --output map.png --zoom 12
        """,
    )
    parser.add_argument("--input", "-i", required=True, help="Input file (.xlsx/.json/.csv)")
    parser.add_argument("--ak", default=None, help="Baidu Maps AK (or set BAIDU_MAP_AK env var)")
    parser.add_argument("--output", "-o", default="distribution_map.png", help="Output PNG path")
    parser.add_argument("--zoom", "-z", type=int, default=None, help="Map zoom (auto if omitted)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--title", "-t", default=None)
    parser.add_argument("--font", "-f", default=None)
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    ak = load_ak(args.ak)
    items = read_input_data(args.input)
    print(f"Reading: {args.input} → {len(items)} records\n")
    if not items:
        print("Error: No data found"); sys.exit(1)

    results = batch_geocode(items, ak)
    render_distribution_map(
        results, args.output, ak,
        zoom=args.zoom, width=args.width, height=args.height,
        title=args.title, font_path=args.font,
    )


if __name__ == "__main__":
    main()
