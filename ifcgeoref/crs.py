"""Coordinate reference systems: details, suggestions, search and conversions (pyproj)."""
import math
import re
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info
from pyproj.enums import PJType

# Official / most widely used survey grids. Codes only appear as suggestions when
# their area of use actually contains the building location.
NATIONAL_GRIDS = {
    2056: "Swiss national grid",
    28992: "Dutch national grid",
    27700: "British National Grid",
    2157: "Irish national grid",
    2154: "French national grid",
    3812: "Belgian national grid (2008)",
    31370: "Belgian national grid (1972)",
    2169: "Luxembourg national grid",
    **{code: "European ETRS89 UTM grid, official in several countries" for code in range(25828, 25836)},
    31254: "Austrian Gauss-Krüger grid (west)",
    31255: "Austrian Gauss-Krüger grid (central)",
    31256: "Austrian Gauss-Krüger grid (east)",
    3763: "Portuguese national grid",
    2180: "Polish national grid",
    5514: "Czech / Slovak national grid",
    3006: "Swedish national grid",
    3067: "Finnish national grid",
    6707: "Italian national grid (zone 32)",
    6708: "Italian national grid (zone 33)",
    3765: "Croatian national grid",
    3794: "Slovenian national grid",
    23700: "Hungarian national grid",
    3844: "Romanian national grid",
    2100: "Greek national grid",
    3301: "Estonian national grid",
    3059: "Latvian national grid",
    3346: "Lithuanian national grid",
    2193: "New Zealand national grid",
    3414: "Singapore national grid",
    2326: "Hong Kong national grid",
    2039: "Israeli national grid",
    **{code: "Australian MGA grid" for code in range(7849, 7860)},
}

# Country extents (south, west, north, east). PROJ only stores a rough rectangle per
# system, so e.g. the French grid's rectangle reaches London; these keep national
# grids to their own country.
COUNTRY_BOUNDS = {
    2056: (45.8, 5.9, 47.9, 10.6), 28992: (50.7, 3.2, 53.7, 7.3), 27700: (49.8, -8.8, 61.0, 2.0),
    2157: (51.3, -10.7, 55.5, -5.3), 2154: [(42.3, -5.3, 51.2, 8.3), (41.3, 8.5, 43.1, 9.6)],
    3812: (49.4, 2.5, 51.6, 6.5),
    31370: (49.4, 2.5, 51.6, 6.5), 2169: (49.4, 5.7, 50.2, 6.6), 31254: (46.3, 9.5, 49.1, 17.2),
    31255: (46.3, 9.5, 49.1, 17.2), 31256: (46.3, 9.5, 49.1, 17.2), 3763: (36.9, -9.6, 42.2, -6.1),
    2180: (49.0, 14.1, 54.9, 24.2), 5514: (47.7, 12.0, 51.1, 22.6), 3006: (55.3, 10.9, 69.1, 24.2),
    3067: (59.7, 19.0, 70.1, 31.6), 6707: (35.4, 6.6, 47.1, 18.6), 6708: (35.4, 6.6, 47.1, 18.6),
    3765: (42.3, 13.4, 46.6, 19.5), 3794: (45.4, 13.3, 46.9, 16.6), 23700: (45.7, 16.1, 48.6, 22.9),
    3844: (43.6, 20.2, 48.3, 29.8), 2100: (34.8, 19.3, 41.8, 29.7), 3301: (57.5, 21.7, 59.8, 28.3),
    3059: (55.6, 20.9, 58.1, 28.3), 3346: (53.9, 20.9, 56.5, 26.9), 2193: (-47.4, 166.3, -34.1, 178.6),
    3414: (1.1, 103.6, 1.5, 104.1), 2326: (22.1, 113.8, 22.6, 114.5), 2039: (29.4, 34.2, 33.4, 35.9),
}

# Geographic/geocentric/odd variants that are never useful for BIM georeferencing.
_SKIP_NAME = re.compile(
    r"south orientated|\(deprecated\)|\bLat-?Long\b|geocentric|approximation|\(3D\)|"
    r"Lambert Azimuthal|LAEA|\bLCC Europe\b|Pseudo-Mercator|Popular Visualisation|World Mercator",
    re.IGNORECASE,
)
_UTM = re.compile(r"/ UTM zone (\d+)([NS])", re.IGNORECASE)


class CRSError(ValueError):
    pass


@lru_cache(maxsize=512)
def _crs(code):
    try:
        crs = CRS.from_epsg(int(code))
    except Exception as exc:  # pyproj raises CRSError with long messages
        raise CRSError(f"EPSG:{code} is not a known coordinate system.") from exc
    return crs


@lru_cache(maxsize=512)
def details(code):
    """Facts about one EPSG code, JSON-serialisable."""
    crs = _crs(code)
    axis = crs.axis_info[0] if crs.axis_info else None
    area = crs.area_of_use
    zone = None
    m = _UTM.search(crs.name)
    if m:
        zone = f"{m.group(1)}{m.group(2).upper()}"
    try:
        method = crs.coordinate_operation.method_name if crs.coordinate_operation else None
    except Exception:
        method = None
    return {
        "code": int(code),
        "id": f"EPSG:{int(code)}",
        "name": crs.name,
        "projected": bool(crs.is_projected),
        "unit": axis.unit_name if axis else None,
        "unit_metres": float(axis.unit_conversion_factor) if axis else None,
        "datum": crs.datum.name if crs.datum else None,
        "projection": method,
        "zone": zone,
        "deprecated": bool(getattr(crs, "is_deprecated", False)),
        "area": None if area is None else {
            "name": area.name,
            "west": area.west, "south": area.south, "east": area.east, "north": area.north,
        },
    }


def require_projected(code):
    info = details(code)
    if not info["projected"]:
        raise CRSError(
            f"{info['id']} ({info['name']}) uses latitude/longitude. "
            "IFC georeferencing needs a projected (map grid) coordinate system in metres or feet."
        )
    return info


def covers(info, lat, lon, margin_deg=0.0):
    """True if the CRS area of use contains the point (None when unknown)."""
    area = info.get("area")
    if area is None or lat is None or lon is None:
        return None
    if not (area["south"] - margin_deg <= lat <= area["north"] + margin_deg):
        return False
    west, east = area["west"] - margin_deg, area["east"] + margin_deg
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east  # area crosses the antimeridian


def utm_code(lat, lon):
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=1)
def _projected_catalogue():
    return [
        info for info in query_crs_info(
            auth_name="EPSG", pj_types=PJType.PROJECTED_CRS, allow_deprecated=False)
        if not _SKIP_NAME.search(info.name)
    ]


def _area_size(info):
    a = info.area_of_use
    if a is None:
        return 1e9
    width = (a.east - a.west) % 360 or 360
    return width * max(a.north - a.south, 1e-6)


@lru_cache(maxsize=256)
def _suggest_cached(lat_r, lon_r, limit):
    lat, lon = lat_r, lon_r
    candidates = query_crs_info(
        auth_name="EPSG", pj_types=PJType.PROJECTED_CRS, allow_deprecated=False,
        area_of_interest=AreaOfInterest(lon, lat, lon, lat), contains=True)
    candidates = [c for c in candidates if not _SKIP_NAME.search(c.name)]

    picks, seen = [], set()

    def add(code, reason, recommended):
        if code in seen or len(picks) >= limit:
            return
        try:
            info = details(code)
        except CRSError:
            return
        if not info["projected"] or covers(info, lat, lon) is False:
            return
        seen.add(code)
        picks.append({**info, "reason": reason, "recommended": recommended})

    def in_country(code):
        bounds = COUNTRY_BOUNDS.get(code)
        if bounds is None:
            return True
        boxes = bounds if isinstance(bounds, list) else [bounds]
        return any(s <= lat <= n and w <= lon <= e for s, w, n, e in boxes)

    # 1. official national grids that cover the point, smallest area first. Once the
    #    point's own country is known, other countries' grids are dropped (shared
    #    European / Australian zone grids stay).
    national = sorted((c for c in candidates if int(c.code) in NATIONAL_GRIDS and in_country(int(c.code))),
                      key=_area_size)
    shared = ("European ETRS89", "Australian MGA")
    country = next((NATIONAL_GRIDS[int(c.code)] for c in national
                    if not NATIONAL_GRIDS[int(c.code)].startswith(shared)), None)
    for c in national:
        label = NATIONAL_GRIDS[int(c.code)]
        if country and not label.startswith(shared) and label.split(" grid")[0] != country.split(" grid")[0]:
            continue
        add(int(c.code), label, not picks)  # only the most local one is "recommended"
    # 2. the worldwide UTM zone of the point
    zone = utm_code(lat, lon)
    add(zone, "UTM zone for this location (works worldwide)", not picks)
    # 3. other regional systems in metres, most local first. Systems without the usual
    #    "Datum / Projection" name are project-specific engineering grids (e.g. rail
    #    "snake" grids) and are left to the search. When a national grid matched, only
    #    systems whose area mentions the same country are offered.
    countries = []
    if picks and picks[0]["code"] in NATIONAL_GRIDS and picks[0].get("area"):
        countries = [part.strip(" .") for part in re.split(r";| - ", picks[0]["area"]["name"])
                     if part.strip(" .") and part.strip()[0].isupper() and len(part) <= 40]
    for c in sorted(candidates, key=_area_size):
        if len(picks) >= limit:
            break
        code = int(c.code)
        if code in seen or code in NATIONAL_GRIDS or " / " not in c.name:
            continue
        area_name = c.area_of_use.name if c.area_of_use else ""
        if countries and not any(country in area_name for country in countries):
            continue
        try:
            unit = details(code)["unit_metres"] or 0
        except CRSError:
            continue
        if abs(unit - 1.0) > 1e-9:
            continue
        add(code, "Regional system covering this location", False)
    return picks


def suggest(lat, lon, limit=8):
    """Ranked EPSG suggestions for a WGS84 location (most suitable first)."""
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return []
    return [dict(p) for p in _suggest_cached(round(lat, 3), round(lon, 3), limit)]


def search(query, lat=None, lon=None, limit=20):
    """Find projected EPSG systems by code or by words in their name / area."""
    query = (query or "").strip()
    if not query:
        return []
    results = []
    code_match = re.fullmatch(r"(?:epsg[:\s]*)?(\d{4,6})", query, re.IGNORECASE)
    if code_match:
        try:
            results.append(dict(details(int(code_match.group(1)))))
        except CRSError:
            pass
    words = [w.lower() for w in re.split(r"\s+", query) if w]
    for item in _projected_catalogue():
        if len(results) >= limit * 3:
            break
        haystack = f"{item.code} {item.name} {item.area_of_use.name if item.area_of_use else ''}".lower()
        if all(w in haystack for w in words) and not any(r["code"] == int(item.code) for r in results):
            try:
                results.append(dict(details(int(item.code))))
            except CRSError:
                continue
    for r in results:
        r["covers_location"] = covers(r, lat, lon)
    # systems covering the building first, then shorter (more specific) names
    results.sort(key=lambda r: (r["covers_location"] is False, not r["projected"], len(r["name"])))
    return results[:limit]


@lru_cache(maxsize=64)
def _to_grid(code):
    return Transformer.from_crs(4326, int(code), always_xy=True)


@lru_cache(maxsize=64)
def _to_wgs84(code):
    return Transformer.from_crs(int(code), 4326, always_xy=True)


def _finite(values):
    return all(math.isfinite(v) for v in values)


def to_grid(code, lonlats):
    """[(lon, lat)] -> [(easting, northing)] in the CRS units."""
    t = _to_grid(code)
    out = []
    for lon, lat in lonlats:
        e, n = t.transform(lon, lat)
        out.append((e, n) if _finite((e, n)) else (None, None))
    return out


def to_wgs84(code, ens):
    """[(easting, northing)] -> [(lon, lat)]."""
    t = _to_wgs84(code)
    out = []
    for e, n in ens:
        lon, lat = t.transform(e, n)
        out.append((lon, lat) if _finite((lon, lat)) else (None, None))
    return out


@lru_cache(maxsize=1)
def _to_mercator():
    return Transformer.from_crs(4326, 3857, always_xy=True)


def to_mercator(lonlats):
    """[(lon, lat)] -> [(x, y)] in Web Mercator metres (the projection of web maps)."""
    t = _to_mercator()
    return [t.transform(lon, lat) for lon, lat in lonlats]


def true_north_in_grid(code, lat, lon):
    """Angle (radians, counter-clockwise from the grid easting axis) of true north
    at the given location. 90 degrees when grid north and true north coincide."""
    t = _to_grid(code)
    d = 1e-5
    e0, n0 = t.transform(lon, lat - d)
    e1, n1 = t.transform(lon, lat + d)
    if not _finite((e0, n0, e1, n1)):
        return math.pi / 2
    return math.atan2(n1 - n0, e1 - e0)
