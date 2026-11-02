"""Turn the user's placement choices into a solution and a map preview."""
import math
import time

from . import crs as crs_mod
from . import solver

UNIT_FACTORS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
UNIT_NAMES = {"mm": "mm", "cm": "cm", "m": "m", "in": "in", "ft": "ft"}
SCALE_SOURCES = {"units", "file", "distance", "manual", "points"}  # "points": a fitted value kept fixed


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


def _xy(values, name):
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        raise PlacementError(f"{name} needs two coordinates.")
    return [_number(values[0], f"{name} X"), _number(values[1], f"{name} Y")]


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
        if len(local) < 2 or len(target) < 2 or None in local[:2] or None in target[:2]:
            continue  # incomplete row in the UI: ignore until both sides are set
        x, y = _number(local[0], f"{label} X"), _number(local[1], f"{label} Y")
        z = _number(local[2] if len(local) > 2 else 0, f"{label} Z", required=False) or 0.0
        e, n = _number(target[0], f"{label} Easting"), _number(target[1], f"{label} Northing")
        h = _number(target[2] if len(target) > 2 else None, f"{label} height", required=False)
        points.append({"local": [x, y, z], "target": [e, n, h], "label": label, "source": "user"})
    return points


def _units(meta, payload, crs_info):
    """Declared file unit, the effective one (user override) and the rigid scale."""
    declared = meta["unit"]
    override = payload.get("unit_override")
    if override not in UNIT_FACTORS:
        override = None
    file_unit_m = UNIT_FACTORS[override] if override else declared["metres"]
    crs_unit = crs_info["unit_metres"] or 1.0
    return {
        "declared_symbol": declared["symbol"],
        "declared_metres": declared["metres"],
        "override": override,
        "symbol": UNIT_NAMES[override] if override else declared["symbol"],
        "metres": file_unit_m,
        "unit_scale": file_unit_m / crs_unit,
        "declared_unit_scale": declared["metres"] / crs_unit,
    }


def _scale_spec(payload, units, crs_info):
    """(mode, value, source) of the requested scale."""
    spec = payload.get("scale")
    if spec is None and payload.get("scale_mode") == "free":  # API v1 compatibility
        spec = {"mode": "free"}
    spec = spec or {}
    if spec.get("mode") == "free":
        return "free", _number(spec.get("value"), "Scale", required=False), spec.get("source") or "units"
    source = spec.get("source") if spec.get("source") in SCALE_SOURCES else "units"
    distance = spec.get("from_distance")
    if distance:
        try:
            value = solver.scale_from_distance(distance.get("p1") or [], distance.get("p2") or [],
                                               _number(distance.get("distance_m"), "Distance"),
                                               crs_info["unit_metres"] or 1.0)
        except ValueError as exc:
            raise PlacementError(str(exc)) from None
        return "fixed", value, "distance"
    value = _number(spec.get("value"), "Scale", required=False)
    if value is not None and value <= 0:
        raise PlacementError("The scale must be greater than 0.")
    if source == "units" or value is None:
        return "fixed", None, "units"
    return "fixed", value, source


def scale_label(source, units, value):
    if source == "units":
        note = " (unit override)" if units["override"] else ""
        return f"File units ({units['symbol']} → {units['unit_scale']:.10g}){note}"
    return {"file": "From file", "points": "Fitted from points", "distance": "From distance",
            "manual": "Manual"}.get(source, source)


def _anchor(payload):
    raw = payload.get("anchor")
    if not raw:
        return None
    return {"local": _xy(raw.get("local"), "Anchor model point"), "map": _xy(raw.get("map"), "Anchor map point")}


def _location(meta, payload, points, anchor, code):
    """Where the model is placed: used to check the CRS covers it and for grid north."""
    # A manual position wins: the model may be dragged far from the site stored in the file.
    ref = anchor["map"] if anchor else (points[0]["target"][:2] if points else None)
    if ref:
        lon, lat = crs_mod.to_wgs84(code, [tuple(ref)])[0]
        if lat is not None:
            return lat, lon, "manual" if anchor else "points"
    site = meta.get("site") or {}
    if site.get("has_location"):
        return site["latitude"], site["longitude"], "ifc"
    loc = payload.get("location") or {}
    if loc.get("lat") is not None and loc.get("lon") is not None:
        return float(loc["lat"]), float(loc["lon"]), "user"
    return None, None, None


def footprint_local(geometry, units, mode="building"):
    """Footprint (file units) and its centre, used as the pivot for manual moves."""
    geometry = geometry or {}
    metres = units["declared_metres"]  # geometry is in metres derived from the declared unit
    outline = geometry.get("footprint_all_m") if mode == "all" else None
    outline = outline or geometry.get("footprint_m")
    if not outline:
        return None, [0.0, 0.0]
    pts = [[x / metres, y / metres] for x, y in outline]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return pts, [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]


def compute(meta, geometry, payload):
    if not meta.get("supported"):
        raise PlacementError("This file cannot be georeferenced (see the messages on the upload step).")
    try:
        code = int(payload.get("epsg"))
    except (TypeError, ValueError):
        raise PlacementError("Choose a coordinate system first.") from None
    crs_info = crs_mod.require_projected(code)
    crs_unit = crs_info["unit_metres"] or 1.0
    units = _units(meta, payload, crs_info)

    points = collect_points(meta, payload, code, crs_info)
    anchor = _anchor(payload)
    if not points and anchor is None:
        raise PlacementError("Add a control point, place the model on the map by hand, "
                             "or use the location stored in the IFC file.")

    lat, lon, location_source = _location(meta, payload, points, anchor, code)
    covers = crs_mod.covers(crs_info, lat, lon)

    north = (meta.get("context") or {}).get("true_north") or [0.0, 1.0]
    alpha = math.atan2(north[1], north[0])
    beta = crs_mod.true_north_in_grid(code, lat, lon) if lat is not None else math.pi / 2
    rotation_fallback = beta - alpha

    mode, value, source = _scale_spec(payload, units, crs_info)
    rotation = _number(payload.get("rotation_deg"), "Rotation", required=False)
    height = _number(payload.get("height"), "Height", required=False)
    offset = payload.get("offset") or [0, 0, 0]
    offset = [_number(v, "Offset", required=False) or 0.0 for v in (list(offset) + [0, 0, 0])[:3]]

    try:
        solution = solver.solve(
            points, unit_scale=units["unit_scale"], rotation_fallback=rotation_fallback, crs_unit_m=crs_unit,
            scale_mode=mode, scale_value=value, scale_source=source, rotation_override=rotation,
            height_override=height, anchor=anchor, offset=offset)
    except ValueError as exc:
        raise PlacementError(str(exc)) from None
    solution["scale_label"] = scale_label(solution["scale_source"], units, solution["scale"])

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

    footprint, pivot = footprint_local(geometry, units, payload.get("footprint_mode") or "building")
    if footprint is None and meta.get("placement_bounds"):
        centre = meta["placement_bounds"]["center"]
        pivot = [centre[0], centre[1]]
    pivot_map = solver.apply(solution, [(pivot[0], pivot[1], 0.0)])[0]
    return {
        "solution": solution,
        "crs": crs_info,
        "unit_scale": units["unit_scale"],
        "units": units,
        "location": {"lat": lat, "lon": lon, "source": location_source},
        "covers_location": covers,
        "points": [{k: p[k] for k in ("label", "source", "local", "target")} for p in points],
        "preview": _preview(meta, geometry, solution, code, units, points, footprint),
        "client": {
            "pivot_local": pivot,
            "pivot_map": [pivot_map[0], pivot_map[1]],
            "footprint_local": footprint,
            "rotation_fallback_deg": solver.normalise_deg(math.degrees(rotation_fallback)),
            "grid_to_mercator": crs_mod.grid_to_mercator(code, pivot_map[0], pivot_map[1]),
            "crs_unit_m": crs_unit,
        },
        "gdh": gdh_record(code, payload, solution, units),
        "can_save": not blocking,
    }


def gdh_record(code, payload, solution, units):
    """What is stored in the GDH_Georeferencing property set, so the placement can be re-edited."""
    return {
        "tool": "ifc-georef",
        "format": 1,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "epsg": code,
        "use_site": bool(payload.get("use_site")),
        "points": [{"label": p.get("label"), "local": p.get("local"), "target": p.get("target")}
                   for p in (payload.get("points") or []) if p.get("local") and p.get("target")],
        "anchor": payload.get("anchor") or None,
        "rotation": {"override_deg": payload.get("rotation_deg"), "value_deg": solution["rotation_deg"],
                     "source": solution["rotation_source"]},
        "scale": {"value": solution["scale"], "source": solution["scale_source"],
                  "mode": "free" if (payload.get("scale") or {}).get("mode") == "free" else "fixed",
                  "unit_override": units["override"]},
        "height": {"override": payload.get("height"), "value": solution["orthogonal_height"],
                   "source": solution["height_source"]},
        "footprint_mode": payload.get("footprint_mode") or "building",
    }


def _to_latlon(code, solution, local_points):
    grid = solver.apply(solution, local_points)
    lonlats = crs_mod.to_wgs84(code, [(e, n) for e, n, _ in grid])
    return [None if lat is None else [lat, lon] for lon, lat in lonlats]


def _map3d(geometry, solution, code, units):
    """Where and how to draw the 3D preview model (model.glb) on a web map.

    The glTF is centred on geometry "offset_m", so it is anchored at that IFC point.
    heading_deg is the direction of the model's +X axis, counter-clockwise from east,
    in Web Mercator — i.e. the rotation a web map needs, grid convergence included.
    """
    offset = geometry.get("offset_m")
    if not offset or geometry.get("empty"):
        return None
    unit_m = units["declared_metres"]
    span = max(max((geometry.get("size_m") or [10.0, 10.0])[:2]), 10.0) / unit_m
    ax, ay = offset[0] / unit_m, offset[1] / unit_m
    anchor, x_end = _to_latlon(code, solution, [(ax, ay, 0.0), (ax + span, ay, 0.0)])
    if not anchor or not x_end:
        return None
    (x0, y0), (x1, y1) = crs_mod.to_mercator([(anchor[1], anchor[0]), (x_end[1], x_end[0])])
    return {
        "anchor_local": [ax, ay],
        "anchor": anchor,
        "heading_deg": math.degrees(math.atan2(y1 - y0, x1 - x0)),
        "scale_ratio": solution["scale"] / units["declared_unit_scale"] if units["declared_unit_scale"] else 1.0,
        "base_z_m": (geometry["bounds_m"]["min"][2] - offset[2]) if geometry.get("bounds_m") else 0.0,
    }


def _preview(meta, geometry, solution, code, units, points, footprint):
    geometry = geometry or {}
    unit_m = units["declared_metres"]
    size_m = max((geometry.get("size_m") or [0, 0])[:2] or [0]) or 20.0
    axis = max(5.0, 0.3 * size_m) / unit_m
    footprint_ll = _to_latlon(code, solution, [(x, y, 0.0) for x, y in footprint]) if footprint else None
    origin, x_end, y_end = _to_latlon(code, solution, [(0, 0, 0), (axis, 0, 0), (0, axis, 0)])
    fitted = _to_latlon(code, solution, [tuple(p["local"]) for p in points])
    targets = crs_mod.to_wgs84(code, [tuple(p["target"][:2]) for p in points])
    out_points = []
    for p, fit, (lon, lat) in zip(points, fitted, targets):
        out_points.append({"label": p["label"], "source": p["source"],
                           "target": None if lat is None else [lat, lon], "fitted": fit})
    coords = [c for c in (footprint_ll or []) + [origin] if c]
    bounds = None
    if coords:
        lats, lons = [c[0] for c in coords], [c[1] for c in coords]
        bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    return {"footprint": footprint_ll, "origin": origin, "x_axis": [origin, x_end],
            "y_axis": [origin, y_end], "axis_local": axis, "points": out_points, "bounds": bounds,
            "map3d": _map3d(geometry, solution, code, units)}
