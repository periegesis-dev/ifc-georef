"""Find the IfcMapConversion parameters from control points and manual placement.

The model is a 2D similarity transform plus a height offset, exactly what
IfcMapConversion stores:

    E = scale * (cos(r) * x - sin(r) * y) + eastings
    N = scale * (sin(r) * x + cos(r) * y) + northings
    H = scale * z + orthogonal_height

Local coordinates are in IFC file units, targets in map (CRS) units. The fit is
closed-form least squares, so there is no initial guess that can go wrong.

Manual placement is expressed with an *anchor*: one local point (x, y) pinned to
one map position (E, N). With an anchor the translation follows from the anchor,
so rotating or scaling keeps the anchored point fixed (used for rotating about the
footprint centre, and for keeping the first point fixed in scale-by-distance).
An anchor also allows placement with no control points at all.

static/js/engine.js is a line-by-line port of this module for the live preview;
keep the two in step.
"""
import math

import numpy as np

MIN_ROTATION_BASE_M = 1.0   # control points closer than this cannot define a rotation
SCALE_TOLERANCE = 0.01      # warn when the scale differs >1 % from the unit conversion


def _warning(code, message, level="warning"):
    return {"code": code, "level": level, "message": message}


def _rotation_from_points(p, q):
    """Best rotation between centred point sets (radians)."""
    a = float(np.sum(p[:, 0] * q[:, 0] + p[:, 1] * q[:, 1]))
    b = float(np.sum(p[:, 0] * q[:, 1] - p[:, 1] * q[:, 0]))
    return math.atan2(b, a)


def normalise_deg(deg):
    deg = math.fmod(deg, 360.0)
    if deg <= -180:
        deg += 360
    elif deg > 180:
        deg -= 360
    return deg


def point_spread_m(points, unit_scale, crs_unit_m):
    """Largest distance between control points, in metres (0 for fewer than 2)."""
    if len(points) < 2:
        return 0.0
    local = np.array([p["local"][:2] for p in points], dtype=float)
    p = local - local.mean(axis=0)
    return float(np.max(np.linalg.norm(p, axis=1))) * 2 * unit_scale * crs_unit_m


def solve(points, *, unit_scale, rotation_fallback, crs_unit_m=1.0, scale_mode="fixed",
          scale_value=None, scale_source=None, rotation_override=None, height_override=None,
          anchor=None, offset=(0.0, 0.0, 0.0)):
    """Solve the map conversion.

    points: list of dicts {"local": [x, y, z], "target": [e, n, h|None], "label": str}
    unit_scale: file units -> map units (the scale of a rigid placement)
    rotation_fallback: rotation (radians) to use when points cannot define it
    crs_unit_m: metres per map unit (only used to express distances in metres)
    scale_mode: "fixed" (scale_value, default unit_scale) or "free" (fitted from points)
    anchor: {"local": [x, y], "map": [E, N]} pins one local point to one map position
    offset: extra (dE, dN, dH) added at the end (kept for API compatibility)
    """
    if not points and anchor is None:
        raise ValueError("At least one control point or a manual placement is needed.")
    warnings = []
    n = len(points)
    local = np.array([p["local"] for p in points], dtype=float).reshape(n, 3)
    target_xy = np.array([p["target"][:2] for p in points], dtype=float).reshape(n, 2)
    if not np.all(np.isfinite(local)) or not np.all(np.isfinite(target_xy)):
        raise ValueError("Control points contain missing or invalid numbers.")

    if n:
        centroid_local = local[:, :2].mean(axis=0)
        centroid_target = target_xy.mean(axis=0)
    else:
        centroid_local = centroid_target = np.zeros(2)
    p = local[:, :2] - centroid_local
    q = target_xy - centroid_target

    spread_m = point_spread_m(points, unit_scale, crs_unit_m)
    can_fit_rotation = n >= 2 and spread_m >= MIN_ROTATION_BASE_M
    if rotation_override is not None:
        rotation, rotation_source = math.radians(rotation_override), "manual"
    elif can_fit_rotation:
        rotation, rotation_source = _rotation_from_points(p, q), "points"
    else:
        rotation, rotation_source = rotation_fallback, "true_north"
        if n >= 2:
            warnings.append(_warning(
                "points_close",
                "The control points are less than 1 m apart, so they cannot define the rotation. "
                "The north direction stored in the IFC file is used instead."))

    fixed_scale = float(scale_value) if scale_value else unit_scale
    scale, source = fixed_scale, (scale_source or ("units" if not scale_value else "manual"))
    if scale_mode == "free":
        if not can_fit_rotation:
            warnings.append(_warning(
                "free_scale_needs_points",
                "Fitting the scale needs at least 2 control points that are well apart. "
                "The current scale is kept.", "notice"))
        else:
            c, s = math.cos(rotation), math.sin(rotation)
            denom = float(np.sum(p ** 2))
            numer = float(np.sum(c * (p[:, 0] * q[:, 0] + p[:, 1] * q[:, 1])
                                 + s * (p[:, 0] * q[:, 1] - p[:, 1] * q[:, 0])))
            if denom > 0 and numer > 0:
                scale, source = numer / denom, "points"
    deviation = abs(scale / unit_scale - 1) if unit_scale else 0.0
    if deviation > SCALE_TOLERANCE:
        reason = ("This usually means a control point is wrong." if source == "points"
                  else "Check that this is intended; a correct file needs only the unit conversion.")
        warnings.append(_warning(
            "scale_deviation",
            f"The scale differs {deviation * 100:.1f} % from the file units. {reason}"))

    c, s = math.cos(rotation), math.sin(rotation)
    rot = np.array([[c, -s], [s, c]])
    if anchor is not None:
        a_local = np.array(anchor["local"][:2], dtype=float)
        a_map = np.array(anchor["map"][:2], dtype=float)
        eastings, northings = a_map - scale * rot @ a_local
        position_source = "manual"
    else:
        eastings, northings = centroid_target - scale * rot @ centroid_local
        position_source = "points"

    heights = [(i, pt["target"][2]) for i, pt in enumerate(points)
               if len(pt["target"]) > 2 and pt["target"][2] is not None]
    if height_override is not None:
        height, height_source = float(height_override), "manual"
    elif heights:
        height = float(np.mean([h - scale * local[i, 2] for i, h in heights]))
        height_source = "points"
    else:
        height, height_source = 0.0, "default"
        warnings.append(_warning(
            "no_height", "No height is known yet, so the model origin is placed at height 0. "
                         "Set the height in the Height tab.", "notice"))

    eastings += offset[0]
    northings += offset[1]
    height += offset[2]

    residuals = []
    for i, pt in enumerate(points):
        fitted = scale * rot @ local[i, :2] + np.array([eastings, northings])
        dx, dy = (fitted - target_xy[i]) * crs_unit_m
        dz = None
        if len(pt["target"]) > 2 and pt["target"][2] is not None:
            dz = (scale * local[i, 2] + height - pt["target"][2]) * crs_unit_m
        residuals.append({
            "label": pt.get("label") or f"P{i + 1}",
            "dx": float(dx), "dy": float(dy),
            "dz": None if dz is None else float(dz),
            "horizontal": float(math.hypot(dx, dy)),
        })

    rms_h = rms_v = None
    if residuals:
        rms_h = math.sqrt(sum(r["horizontal"] ** 2 for r in residuals) / len(residuals))
        vertical = [r["dz"] for r in residuals if r["dz"] is not None]
        rms_v = math.sqrt(sum(v * v for v in vertical) / len(vertical)) if vertical else None
    fitted_points = n >= 2 and (rotation_source == "points" or source == "points")
    if fitted_points and rms_h > 2.0:
        warnings.append(_warning(
            "low_accuracy", f"The control points disagree by {rms_h:.2f} m on average. "
                            "Check that each model point matches its map point."))
    elif fitted_points and rms_h > 0.2:
        warnings.append(_warning(
            "medium_accuracy", f"The control points disagree by {rms_h:.2f} m on average.", "notice"))

    deg = normalise_deg(math.degrees(rotation))
    return {
        "eastings": float(eastings),
        "northings": float(northings),
        "orthogonal_height": float(height),
        "rotation_deg": deg,
        "rotation_cw_deg": normalise_deg(-deg),
        "x_axis_abscissa": math.cos(math.radians(deg)),
        "x_axis_ordinate": math.sin(math.radians(deg)),
        "scale": float(scale),
        "rotation_source": rotation_source,
        "scale_source": source,
        "height_source": height_source,
        "position_source": position_source,
        "can_fit_rotation": bool(can_fit_rotation),
        "residuals": residuals,
        "rms_horizontal_m": rms_h,
        "rms_vertical_m": rms_v,
        "warnings": warnings,
    }


def apply(solution, local_xyz):
    """Transform local file coordinates with a solution -> (E, N, H)."""
    c, s = solution["x_axis_abscissa"], solution["x_axis_ordinate"]
    k = solution["scale"]
    out = []
    for x, y, z in local_xyz:
        out.append((k * (c * x - s * y) + solution["eastings"],
                    k * (s * x + c * y) + solution["northings"],
                    k * z + solution["orthogonal_height"]))
    return out


def scale_from_distance(p1, p2, distance_m, crs_unit_m):
    """Scale that makes the model distance p1-p2 (file units) equal distance_m metres."""
    d_file = math.dist([float(v) for v in p1[:3]], [float(v) for v in p2[:3]])
    if d_file <= 0:
        raise ValueError("The two points are at the same place in the model.")
    if not distance_m or distance_m <= 0:
        raise ValueError("The true distance must be greater than 0.")
    return (float(distance_m) / crs_unit_m) / d_file
