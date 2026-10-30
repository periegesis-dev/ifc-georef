"""Turn the user's placement choices into a solution and a map preview."""
import math

from . import crs as crs_mod
from . import solver


class PlacementError(ValueError):
    pass


def _number(value, name, required=True):
    if value is None or value == "":
        if required:
            raise PlacementError(f"{name} is missing.")
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise PlacementError(f"{name} must be a number.") from None
    if not math.isfinite(value):
        raise PlacementError(f"{name} must be a finite number.")
    return value


def _location(meta, payload, points, code):
    site = meta.get("site") or {}
    if site.get("has_location"):
        return site["latitude"], site["longitude"], "ifc"
    loc = payload.get("location") or {}
    if loc.get("lat") is not None and loc.get("lon") is not None:
        return float(loc["lat"]), float(loc["lon"]), "user"
    if points:
        lon, lat = crs_mod.to_wgs84(code, [points[0]["target"][:2]])[0]
        if lat is not None:
            return lat, lon, "points"
    return None, None, None


def collect_points(meta, payload, code, crs_info):
    crs_unit = crs_info["unit_metres"] or 1.0
    points = []
    site = meta.get("site") or {}
    if payload.get("use_site") and site.get("has_location"):
        e, n = crs_mod.to_grid(code, [(site["longitude"], site["latitude"])])[0]
        if e is None:
            raise PlacementError(f"The site location cannot be expressed in {crs_info['id']}.")
        h = site["elevation_m"] / crs_unit if site.get("elevation_m") is not None else None
        points.append({"local": list(site.get("origin") or [0, 0, 0]), "target": [e, n, h],
                       "label": "IFC site", "source": "site"})
    for i, raw in enumerate(payload.get("points") or []):
        label = str(raw.get("label") or f"P{i + 1}")[:40]
        local = raw.get("local") or []
        target = raw.get("target") or []
        if len(local) < 2 or len(target) < 2:
            continue  # incomplete row in the UI: ignore until both sides are set
        try:
            x, y = _number(local[0], f"{label} X"), _number(local[1], f"{label} Y")
            z = _number(local[2] if len(local) > 2 else 0, f"{label} Z", required=False) or 0.0
            e, n = _number(target[0], f"{label} Easting"), _number(target[1], f"{label} Northing")
            h = _number(target[2] if len(target) > 2 else None, f"{label} height", required=False)
        except PlacementError:
            raise
        points.append({"local": [x, y, z], "target": [e, n, h], "label": label, "source": "user"})
    return points


def compute(meta, geometry, payload):
    if not meta.get("supported"):
        raise PlacementError("This file cannot be georeferenced (see the messages on the upload step).")
    try:
        code = int(payload.get("epsg"))
    except (TypeError, ValueError):
        raise PlacementError("Choose a coordinate system first.") from None
    crs_info = crs_mod.require_projected(code)
    crs_unit = crs_info["unit_metres"] or 1.0
    unit_m = meta["unit"]["metres"]
    unit_scale = unit_m / crs_unit

    points = collect_points(meta, payload, code, crs_info)
    if not points:
        raise PlacementError("Add at least one control point, or use the location stored in the IFC file.")

    lat, lon, location_source = _location(meta, payload, points, code)
    covers = crs_mod.covers(crs_info, lat, lon)

    north = (meta.get("context") or {}).get("true_north") or [0.0, 1.0]
    alpha = math.atan2(north[1], north[0])
    beta = crs_mod.true_north_in_grid(code, lat, lon) if lat is not None else math.pi / 2
    rotation_fallback = beta - alpha

    rotation = _number(payload.get("rotation_deg"), "Rotation", required=False)
    height = _number(payload.get("height"), "Height", required=False)
    offset = payload.get("offset") or [0, 0, 0]
    offset = [_number(v, "Offset", required=False) or 0.0 for v in (list(offset) + [0, 0, 0])[:3]]

    solution = solver.solve(
        points, unit_scale=unit_scale, rotation_fallback=rotation_fallback, crs_unit_m=crs_unit,
        scale_mode="free" if payload.get("scale_mode") == "free" else "fixed",
        rotation_override=rotation, height_override=height, offset=offset)

    blocking = False
    if covers is False:
        blocking = not payload.get("allow_outside")
        area = (crs_info.get("area") or {}).get("name") or "its area of use"
        solution["warnings"].insert(0, {
            "code": "outside_area", "level": "error",
            "message": f"The building lies outside the area where {crs_info['id']} is valid ({area}). "
                       "Coordinates would be distorted or wrong. Choose a coordinate system that covers it."})
    elif covers is None and lat is None:
        solution["warnings"].append({"code": "unknown_location", "level": "notice",
                                     "message": "The building location is unknown, so the coordinate "
                                                "system could not be checked."})

    return {
        "solution": solution,
        "crs": crs_info,
        "unit_scale": unit_scale,
        "location": {"lat": lat, "lon": lon, "source": location_source},
        "covers_location": covers,
        "points": [{k: p[k] for k in ("label", "source", "local", "target")} for p in points],
        "preview": _preview(meta, geometry, solution, code, unit_m, unit_scale, points),
        "can_save": not blocking,
    }


def _to_latlon(code, solution, local_points):
    grid = solver.apply(solution, local_points)
    lonlats = crs_mod.to_wgs84(code, [(e, n) for e, n, _ in grid])
    return [None if lat is None else [lat, lon] for lon, lat in lonlats]


def _map3d(geometry, solution, code, unit_m, unit_scale):
    """Where and how to draw the 3D preview model (model.glb) on a web map.

    The glTF is centred on geometry "offset_m", so it is anchored at that IFC point.
    heading_deg is the direction of the model's +X axis, counter-clockwise from east,
    in Web Mercator — i.e. the rotation a web map needs, grid convergence included.
    """
    offset = geometry.get("offset_m")
    if not offset or geometry.get("empty"):
        return None
    span = max(max((geometry.get("size_m") or [10.0, 10.0])[:2]), 10.0) / unit_m
    ax, ay = offset[0] / unit_m, offset[1] / unit_m
    anchor, x_end = _to_latlon(code, solution, [(ax, ay, 0.0), (ax + span, ay, 0.0)])
    if not anchor or not x_end:
        return None
    (x0, y0), (x1, y1) = crs_mod.to_mercator([(anchor[1], anchor[0]), (x_end[1], x_end[0])])
    return {
        "anchor": anchor,
        "heading_deg": math.degrees(math.atan2(y1 - y0, x1 - x0)),
        "scale_ratio": solution["scale"] / unit_scale if unit_scale else 1.0,
        "base_z_m": (geometry["bounds_m"]["min"][2] - offset[2]) if geometry.get("bounds_m") else 0.0,
    }


def _preview(meta, geometry, solution, code, unit_m, unit_scale, points):
    geometry = geometry or {}
    size_m = max((geometry.get("size_m") or [0, 0])[:2] or [0]) or 20.0
    axis = max(5.0, 0.3 * size_m) / unit_m
    footprint = None
    if geometry.get("footprint_m"):
        footprint = _to_latlon(code, solution, [(x / unit_m, y / unit_m, 0.0)
                                                for x, y in geometry["footprint_m"]])
    origin, x_end, y_end = _to_latlon(code, solution, [(0, 0, 0), (axis, 0, 0), (0, axis, 0)])
    fitted = _to_latlon(code, solution, [tuple(p["local"]) for p in points])
    targets = crs_mod.to_wgs84(code, [tuple(p["target"][:2]) for p in points])
    out_points = []
    for p, fit, (lon, lat) in zip(points, fitted, targets):
        out_points.append({"label": p["label"], "source": p["source"],
                           "target": None if lat is None else [lat, lon], "fitted": fit})
    coords = [c for c in (footprint or []) + [origin] if c]
    bounds = None
    if coords:
        lats, lons = [c[0] for c in coords], [c[1] for c in coords]
        bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    return {"footprint": footprint, "origin": origin, "x_axis": [origin, x_end],
            "y_axis": [origin, y_end], "points": out_points, "bounds": bounds,
            "map3d": _map3d(geometry, solution, code, unit_m, unit_scale)}
