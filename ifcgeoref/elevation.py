"""Suggested ground height for a location (used to pre-fill control point heights)."""
import logging
from functools import lru_cache

import requests

from . import crs

log = logging.getLogger(__name__)

SWISS_BOUNDS = (45.8, 5.9, 47.9, 10.6)  # south, west, north, east


@lru_cache(maxsize=2048)
def _swisstopo(lat, lon):
    e, n = crs.to_grid(2056, [(lon, lat)])[0]
    if e is None:
        return None
    r = requests.get("https://api3.geo.admin.ch/rest/services/height",
                     params={"easting": round(e, 2), "northing": round(n, 2), "sr": 2056}, timeout=4)
    r.raise_for_status()
    height = float(r.json()["height"])
    return {"height_m": height, "source": "swisstopo terrain model (LN02)"}


@lru_cache(maxsize=2048)
def _opentopodata(lat, lon):
    r = requests.get("https://api.opentopodata.org/v1/mapzen",
                     params={"locations": f"{lat},{lon}"}, timeout=5)
    r.raise_for_status()
    value = r.json()["results"][0]["elevation"]
    if value is None:
        return None
    return {"height_m": float(value), "source": "OpenTopoData (Mapzen terrain, approx.)"}


def lookup(lat, lon, mode="all"):
    """Terrain height in metres, or None when unavailable / disabled."""
    if mode == "off":
        return None
    lat, lon = round(float(lat), 6), round(float(lon), 6)
    south, west, north, east = SWISS_BOUNDS
    try:
        if south <= lat <= north and west <= lon <= east:
            result = _swisstopo(lat, lon)
            if result:
                return result
        if mode == "all":
            return _opentopodata(lat, lon)
    except Exception as exc:
        log.info("elevation lookup failed for %s,%s: %s", lat, lon, exc)
    return None
