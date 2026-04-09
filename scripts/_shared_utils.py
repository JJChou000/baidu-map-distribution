#!/usr/bin/env python3
"""
Shared utilities for baidu-map-distribution skill.
Provides: config loading, API calls, coordinate math, map calibration.

Security: API keys are loaded from environment variables or config files.
NEVER hardcode keys in source code.
"""

import json
import math
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage
import requests


# ============================================================
# Configuration
# ============================================================

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 768

GEOCODE_URL = "https://api.map.baidu.com/geocoding/v3/"
STATIC_MAP_URL = "http://api.map.baidu.com/staticimage/v2/"
DIRECTION_URL = "https://api.map.baidu.com/direction/v2/driving"

# ============================================================
# 全局限流器 — 所有百度API共享，防止叠加超限
# ============================================================
# 百度免费额度（日）：
#   地理编码: ~6,000次/天   静态图: ~100,000次/天
#   路线规划: ~30万次/月(~1万次/天)  地点检索: ~6,000次/天
# 安全策略：全局统一限流 + 各API独立计数

import threading

class _BaiduRateLimiter:
    """线程安全的全局限流器，所有百度API调用必须经过这里"""
    def __init__(self):
        self._lock = threading.Lock()
        self._last_call_time = 0          # 上次任意API调用时间
        self._min_interval = 0.35         # 全局最小间隔(秒) ≈ 2.8 QPS，远低于所有限额
        # 各API独立计数器（用于日志和预警）
        self._counts = {
            "geocode": 0,
            "direction": 0,
            "static_map": 0,
            "place_search": 0,
        }
        # 各API日限额预警阈值（达到此值开始放慢速度）
        self._warnings = {
            "geocode": 4000,
            "direction": 7000,
            "static_map": 80000,
            "place_search": 4000,
        }
    
    def wait(self, api_type="default"):
        """调用前等待，确保不超并发。返回是否接近限额"""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call_time = time.time()
            self._counts[api_type] = self._counts.get(api_type, 0) + 1
            
            count = self._counts[api_type]
            warn_at = self._warnings.get(api_type, float('inf'))
            
            # 接近限额时自动加大间隔
            if count > warn_at * 0.8:
                extra_sleep = min(0.5, (count / warn_at) * 0.3)
                return True, extra_sleep  # 接近上限
            
            return False, 0
    
    def get_stats(self):
        with self._lock:
            return dict(self._counts)

# 全局单例
_rate_limiter = _BaiduRateLimiter()

REQUEST_DELAY = 0.2      # seconds between batch geocoding requests
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 3]


def load_ak(ak=None, config_path=None):
    """
    Load Baidu Maps AK from (in priority order):
      1. Explicit `ak` parameter (CLI --arg)
      2. BAIDU_MAP_AK environment variable
      3. config.json file (next to this script's parent)

    Raises ValueError if no key found.
    """
    if ak:
        return ak

    env_ak = os.environ.get("BAIDU_MAP_AK")
    if env_ak:
        return env_ak

    cfg_path = Path(config_path or DEFAULT_CONFIG_PATH)
    if cfg_path.exists():
        try:
            with open(cfg_path, "r") as f:
                cfg = json.load(f)
            key = cfg.get("ak") or cfg.get("api_key") or cfg.get("BAIDU_MAP_AK")
            if key:
                return key
        except (json.JSONDecodeError, OSError):
            pass

    raise ValueError(
        "No Baidu Maps AK found. Set one of:\n"
        "  1. --ak <YOUR_KEY> CLI argument\n"
        "  2. BAIDU_MAP_AK environment variable\n"
        "  3. config.json with 'ak' field\n"
        f"  (expected config at: {cfg_path})"
    )


def load_config(config_path=None):
    """Load full config dict from JSON file. Returns empty dict if not found."""
    cfg_path = Path(config_path or DEFAULT_CONFIG_PATH)
    if cfg_path.exists():
        try:
            with open(cfg_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ============================================================
# Coordinate Math
# ============================================================

def haversine(lng1, lat1, lng2, lat2):
    """Haversine distance in km between two (lng, lat) points."""
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 6371.0 * 2.0 * math.asin(math.sqrt(a))


def merge_nearby(points, merge_dist_m=300):
    """Merge points within merge_dist_m meters into cluster centroids."""
    if len(points) <= 1:
        return list(points)

    merged = []
    used = [False] * len(points)

    for i, p in enumerate(points):
        if used[i]:
            continue
        cluster = [p]
        used[i] = True
        for j in range(i + 1, len(points)):
            if used[j]:
                continue
            dist_m = haversine(p["lng"], p["lat"],
                               points[j]["lng"], points[j]["lat"]) * 1000
            if dist_m <= merge_dist_m:
                cluster.append(points[j])
                used[j] = True

        n = len(cluster)
        avg_lng = sum(c["lng"] for c in cluster) / n
        avg_lat = sum(c["lat"] for c in cluster) / n
        names = ", ".join(c.get("name", "?")[:12] for c in cluster)

        merged.append({
            "lng": avg_lng,
            "lat": avg_lat,
            "name": f"[{n}家] {names}" if n > 1 else cluster[0].get("name", ""),
            "original_count": n,
            "cluster_members": cluster,
        })

    return merged


# ============================================================
# Baidu API Calls
# ============================================================

def api_geocode(address, ak, max_retries=MAX_RETRIES):
    """
    Geocode single address → (lng, lat, status, formatted_address).
    Coordinates returned as (lng, lat) — standard order.
    Retries on 401/429/5xx.
    """
    near_limit, extra = _rate_limiter.wait("geocode")
    if extra:
        time.sleep(extra)

    params = {"address": address, "output": "json", "ak": ak}

    for attempt in range(max_retries + 1):
        try:
            r = requests.get(GEOCODE_URL, params=params, timeout=10)
            data = r.json()

            if data.get("status") == 0:
                loc = data["result"]["location"]
                return (float(loc["lng"]), float(loc["lat"]),
                        "OK", data.get("result", {}).get("formatted_address", address))

            status_code = data.get("status", 0)
            if status_code in (401, 429) or (status_code >= 500 and attempt < max_retries):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                time.sleep(wait)
                continue

            return (0, 0, f"API_ERROR:{status_code}", address)

        except Exception as e:
            if attempt < max_retries:
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                continue
            return (0, 0, f"EXCEPTION:{e}", address)

    return (0, 0, "RETRY_EXHAUSTED", address)


def api_direction(origin_lat, origin_lng, dest_lat, dest_lng, ak,
                  tactics="11"):
    """
    Baidu Direction API v2 driving route.
    
    **IMPORTANT**: Coordinate order is (lat, lng) — NOT (lng, lat)!
    Returns (distance_m, duration_s) or (0, 0) on failure.
    """
    near_limit, extra = _rate_limiter.wait("direction")
    if extra:
        time.sleep(extra)
    
    for _ in range(MAX_RETRIES + 1):
        try:
            r = requests.get(DIRECTION_URL, params={
                "origin": f"{origin_lat},{origin_lng}",
                "destination": f"{dest_lat},{dest_lng}",
                "ak": ak,
                "tactics_in_city": tactics,
                "coord_type": "bd09ll",
                "output": "json",
            }, timeout=15)
            data = r.json()

            if data.get("status") == 0 and data.get("result", {}).get("routes"):
                route = data["result"]["routes"][0]
                return route["distance"], route["duration"]

            status = data.get("status", 0)
            if status in (401, 429) or status >= 500:
                time.sleep(1); continue

            # Fallback to haversine estimate
            dist = int(haversine(origin_lng, origin_lat, dest_lng, dest_lat) * 1000 * 1.4)
            return dist, int(dist / 25000 * 3600)

        except Exception:
            continue
    return 0, 0


def batch_geocode(items, ak, cache_dir=None):
    """
    Batch geocode with caching. Adds lng/lat/geo_status/geo_address to each item.
    
    Args:
        items: list of dicts with 'address' field
        ak: Baidu Maps AK
        cache_dir: directory for cache file (default: script dir)
    """
    cache_file = os.path.join(
        cache_dir or str(Path(__file__).parent),
        ".geocode_cache.json",
    )
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
        except Exception:
            pass

    results = []
    success = fail = 0

    for i, item in enumerate(items):
        addr = item.get("address", item.get("地址", "")).strip()
        if not addr:
            results.append(dict(item, geo_status="NO_ADDRESS"))
            fail += 1
            continue

        # Cache hit
        if addr in cache:
            c = cache[addr]
            results.append(dict(item, lng=c["lng"], lat=c["lat"],
                                geo_status="OK", geo_address=c.get("address", addr)))
            success += 1
            continue

        # Rate limit
        if i > 0:
            time.sleep(REQUEST_DELAY)

        lng, lat, status, fmt_addr = api_geocode(addr, ak)
        results.append(dict(item, lng=lng, lat=lat,
                            geo_status=status, geo_address=fmt_addr))

        if status == "OK":
            cache[addr] = {"lng": lng, "lat": lat, "address": fmt_addr}
            success += 1
        else:
            fail += 1

        print(f"  [{i+1}/{len(items)}] {item.get('name','?')[:20]:20s} → {status}",
              flush=True)

    # Persist cache
    try:
        with open(cache_file, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    print(f"\nGeocoding: {success} OK, {fail} failed ({len(items)} total)")
    return results


# ============================================================
# Map Calibration (Image Differencing)
# ============================================================

def calibrate_pixel_scale(center_lng, center_lat, zoom, w, h, ak):
    """
    Calibrate coord→pixel mapping via image differencing.
    Returns (ppd_x, ppd_y): pixels per degree for lng/lat.
    """
    url_clean = (f"{STATIC_MAP_URL}?ak={ak}"
                 f"&center={center_lng},{center_lat}&width={w}&height={h}&zoom={zoom}")
    _rate_limiter.wait("static_map")
    img_a = np.array(Image.open(
        BytesIO_wrapper(requests.get(url_clean, timeout=20).content)).convert("RGB"))

    ppd_x = _calibrate_axis(img_a, center_lng, center_lat, zoom, w, h, ak, axis="lng")
    ppd_y = _calibrate_axis(img_a, center_lng, center_lat, zoom, w, h, ak, axis="lat")

    print(f"  Calibration: ppd_lng={ppd_x:.1f}, ppd_lat={ppd_y:.1f} px/deg")
    return ppd_x, ppd_y


def _calibrate_axis(img_clean, clng, clat, zoom, w, h, ak, axis="lng"):
    """Calibrate one axis by placing a test marker and diffing."""
    from io import BytesIO

    if axis == "lng":
        offset_val = 0.5
        param = f"{clng + offset_val},{clat}"
        ref_px = w // 2
        compare_idx = 0  # x coordinate in mean()
    else:
        offset_val = 0.3
        param = f"{clng},{clat - offset_val}"
        ref_px = h // 2
        compare_idx = 1  # y coordinate in mean()

    url_marker = (f"{STATIC_MAP_URL}?ak={ak}"
                  f"&center={clng},{clat}&width={w}&height={h}&zoom={zoom}"
                  f"&markers={param}")
    _rate_limiter.wait("static_map")
    img_marker = np.array(Image.open(
        BytesIO(requests.get(url_marker, timeout=15).content)).convert("RGB"))

    diff = np.abs(img_clean.astype(int) - img_marker.astype(int)).sum(axis=2)
    mask = diff > 30
    labeled, n = ndimage.label(mask)

    best_px = ref_px
    best_size = 0
    for i in range(1, n + 1):
        ys, xs = np.where(labeled == i)
        s = len(xs)
        mean_val = np.mean(xs) if axis == "lng" else np.mean(ys)
        margin = 5
        title_h = 42; bottom_h = 25
        valid = (margin < np.mean(xs) < w - margin and title_h < np.mean(ys) < h - bottom_h)
        if s > best_size and valid:
            best_size = s
            best_px = int(mean_val)

    return abs(best_px - (w // 2 if axis == "lng" else h // 2)) / offset_val


def BytesIO_wrapper(content):
    """Lazy import of BytesIO to avoid circular import issues at module level."""
    from io import BytesIO
    return BytesIO(content)


# ============================================================
# Input Reading
# ============================================================

def safe_path(path_str):
    """Validate and resolve path. Prevents basic path traversal."""
    p = Path(path_str).resolve()
    # Basic check: must be a normal file within reasonable bounds
    if not str(p).startswith(str(Path.cwd().resolve())):
        if not p.is_absolute():
            p = Path.cwd() / path_str
    return str(p)


def read_input_data(path):
    """
    Read addresses from Excel/JSON/CSV.
    Returns list of dicts with 'name' and 'address'.
    Raises ValueError on unsupported format or missing columns.
    """
    path = str(path).strip()

    if not os.path.exists(path):
        raise FileNotFoundError(f"Input file not found: {path}")

    if path.endswith((".xlsx", ".xls")):
        return _read_excel(path)
    elif path.endswith(".json"):
        return _read_json(path)
    elif path.endswith(".csv"):
        return _read_csv(path)
    else:
        raise ValueError(f"Unsupported format: {path}. Use .xlsx, .json, or .csv")


def _read_excel(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Excel file is empty")

    # Auto-detect header row (skip disclaimers)
    header_row_idx = 0
    for idx, row in enumerate(rows):
        first_cell = str(row[0]).strip() if row else ""
        if any(kw in first_cell.lower() for kw in
               ["名称", "name", "企业", "地址", "address", "公司"]):
            header_row_idx = idx
            break
        if len(first_cell) < 30 and idx > 0:
            header_row_idx = idx - 1
            break

    header = [str(c).strip() for c in rows[header_row_idx]]
    name_col = addr_col = None
    for i, h in enumerate(header):
        hl = h.lower()
        if hl in ("name", "名称", "企业名称", "公司名称", "单位名称", "姓名"):
            name_col = i
        elif hl in ("address", "地址", "企业地址", "详细地址", "注册地址", "所在地"):
            addr_col = i

    if addr_col is None:
        raise ValueError(f"No address column in headers: {header}")
    if name_col is None:
        name_col = addr_col

    results = []
    for row in rows[header_row_idx + 1:]:
        if len(row) > max(name_col, addr_col) and row[addr_col]:
            results.append({
                "name": str(row[name_col]) if name_col < len(row) else "",
                "address": str(row[addr_col]),
            })
    return results


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [{"name": item.get("name", item.get("名称", "")),
                 "address": item.get("address", item.get("地址", ""))}
                for item in data if item.get("address") or item.get("地址")]
    raise ValueError("JSON must be an array of objects")


def _read_csv(path):
    import csv
    results = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row.get("address") or row.get("地址", "")
            if addr:
                results.append({"name": row.get("name", row.get("名称", "")), "address": addr})
    return results
