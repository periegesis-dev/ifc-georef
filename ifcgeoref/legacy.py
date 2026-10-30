"""Responses of the original public API, kept field-for-field compatible.

POST /api/analyze-georeferencing is used by the GeoDataHub frontend
(components/Home/ThreeDViewModal.js, lib/ifcGeoreferencing.js).
"""
import math
from collections import Counter

from pyproj import Transformer

from . import model_info


def _units_info(ifc_file):
    out = {"length_unit": None, "area_unit": None, "volume_unit": None, "angle_unit": None}
    assignments = ifc_file.by_type("IfcUnitAssignment")
    if not assignments:
        return out
    keys = {"LENGTHUNIT": "length_unit", "AREAUNIT": "area_unit",
            "VOLUMEUNIT": "volume_unit", "PLANEANGLEUNIT": "angle_unit"}
    for unit in assignments[0].Units or []:
        key = keys.get(getattr(unit, "UnitType", None))
        if not key:
            continue
        if unit.is_a("IfcSIUnit"):
            out[key] = f"{unit.Prefix or ''}{unit.Name}"
        elif unit.is_a("IfcConversionBasedUnit"):
            out[key] = str(unit.Name).upper()
    return out


def analyze(ifc_file, filename):
    meta = model_info.extract(ifc_file)
    georef = meta["georef"]
    site = meta["site"] or {}
    result = {
        "filename": filename,
        "schema": meta["schema"],
        "is_georeferenced": bool(georef.get("present")),
        "georeferencing_method": None,
        "georeferencing_info": {},
        "spatial_structure": meta["spatial_counts"],
        "coordinates_info": {},
        "world_coordinates": {},
        "mapconversion_data": {},
        "site_reference": {},
        "units_info": _units_info(ifc_file),
        "building_elements": {},
        "issues": [{"type": "warning", "message": w} for w in meta["warnings"]],
        "recommendations": [],
    }

    if georef.get("present"):
        result["georeferencing_method"] = ("IFC4+ MapConversion" if meta["schema"] != "IFC2X3"
                                           else "IFC2x3 Property Sets")
        mc = {k: georef.get(k) for k in ("eastings", "northings", "orthogonal_height",
                                         "x_axis_abscissa", "x_axis_ordinate", "scale")}
        crs_name = georef.get("crs_name") or "Unknown CRS"
        rotation = math.atan2(mc["x_axis_ordinate"], mc["x_axis_abscissa"])
        result["georeferencing_info"] = {
            "map_conversion": mc,
            "projected_crs": {"name": crs_name, "description": georef.get("crs_description")},
            "rotation": {"degrees": math.degrees(rotation), "radians": rotation},
        }
        result["mapconversion_data"] = {
            "target_crs": crs_name, "eastings": mc["eastings"], "northings": mc["northings"],
            "orthogonal_height": mc["orthogonal_height"], "scale": mc["scale"],
        }
        epsg = georef.get("epsg")
        if epsg and mc["eastings"] is not None and mc["northings"] is not None:
            try:
                lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(
                    mc["eastings"], mc["northings"])
                height = mc["orthogonal_height"] or 0
                result["world_coordinates"] = {
                    "utm": {"x": mc["eastings"], "y": mc["northings"], "z": height, "zone": epsg},
                    "wgs84": {"latitude": lat, "longitude": lon, "altitude": height},
                    "three_js": {"x": mc["eastings"] * 0.001, "y": height * 0.001,
                                 "z": mc["northings"] * 0.001, "scale": mc["scale"],
                                 "rotation_radians": rotation},
                }
            except Exception:
                result["issues"].append({"type": "warning",
                                         "message": f"Could not convert EPSG:{epsg} coordinates to WGS84."})

    if site:
        result["site_reference"] = {
            "name": site.get("name"), "description": None,
            "ref_latitude": site.get("latitude"), "ref_longitude": site.get("longitude"),
            "ref_elevation": site.get("elevation"),
            "local_placement": dict(zip("xyz", site.get("origin") or [0, 0, 0])),
        }

    bounds = meta.get("placement_bounds")
    if bounds:
        lo, hi, centre = bounds["min"], bounds["max"], bounds["center"]
        result["coordinates_info"] = {
            "total_objects": bounds["count"],
            "bounds": {"min_x": lo[0], "max_x": hi[0], "min_y": lo[1], "max_y": hi[1],
                       "min_z": lo[2], "max_z": hi[2]},
            "dimensions": {"width": hi[0] - lo[0], "depth": hi[1] - lo[1], "height": hi[2] - lo[2]},
            "center": {"x": centre[0], "y": centre[1], "z": centre[2]},
        }

    types = Counter(dict(meta["element_types"]))
    result["building_elements"] = {
        "total_products": meta["product_count"],
        "element_types": dict(Counter(p.is_a() for p in ifc_file.by_type("IfcProduct"))),
        "top_elements": [list(t) for t in types.most_common(10)],
    }

    if georef.get("present"):
        result["recommendations"].append({
            "type": "success",
            "message": f"File is properly georeferenced using {result['georeferencing_method']}"})
    else:
        result["recommendations"].append({
            "type": "error",
            "message": "File is not georeferenced. Consider adding coordinate reference system information."})

    if georef.get("present") and bounds and georef.get("eastings") is not None:
        result["bounding_box_world"] = _bounding_box_world(georef, bounds)
    return result


def _bounding_box_world(georef, bounds):
    lo, hi = bounds["min"], bounds["max"]
    angle = math.atan2(georef["x_axis_ordinate"], georef["x_axis_abscissa"])
    c, s = math.cos(angle), math.sin(angle)
    k = georef["scale"] or 1.0
    e0, n0, h0 = georef["eastings"], georef["northings"], georef["orthogonal_height"] or 0.0
    epsg = georef.get("epsg") or 2056
    try:
        to_wgs84 = Transformer.from_crs(epsg, 4326, always_xy=True)
    except Exception:
        to_wgs84 = None

    corners = []
    for name, x, y in (("bottom_left", lo[0], lo[1]), ("bottom_right", hi[0], lo[1]),
                       ("top_right", hi[0], hi[1]), ("top_left", lo[0], hi[1])):
        e = k * (c * x - s * y) + e0
        n = k * (s * x + c * y) + n0
        lon = lat = None
        if to_wgs84:
            lon, lat = to_wgs84.transform(e, n)
        corners.append({"name": name, "easting": e, "northing": n, "elevation": k * lo[2] + h0,
                        "latitude": lat, "longitude": lon})

    def corner(fn):
        e = fn(c["easting"] for c in corners)
        n = fn(c["northing"] for c in corners)
        lon = lat = None
        if to_wgs84:
            lon, lat = to_wgs84.transform(e, n)
        return e, n, lat, lon

    min_e, min_n, min_lat, min_lon = corner(min)
    max_e, max_n, max_lat, max_lon = corner(max)
    return {
        "corners": corners,
        "min": {"easting": min_e, "northing": min_n, "elevation": k * lo[2] + h0,
                "latitude": min_lat, "longitude": min_lon},
        "max": {"easting": max_e, "northing": max_n, "elevation": k * hi[2] + h0,
                "latitude": max_lat, "longitude": max_lon},
        "crs": georef.get("crs_name") or f"EPSG:{epsg}",
        "description": "Bounding box of the IFC building in world coordinates (Easting/Northing) and WGS84 (Lat/Long)",
    }


def devs_report(ifc_file, filename):
    georef = model_info.georef_info(ifc_file)
    lines = [f"Filename: {filename}", f"Georeferenced: {'YES' if georef.get('present') else 'NO'}",
             f"IFC version: {ifc_file.schema}"]
    if georef.get("present"):
        lines.append("IfcMapConversion:")
        for key in ("crs_name", "eastings", "northings", "orthogonal_height",
                    "x_axis_abscissa", "x_axis_ordinate", "scale"):
            lines.append(f"  {key}: {georef.get(key)}")
    else:
        lines.append("Open this service in a web browser to georeference the file.")
    return "\n".join(lines) + "\n"
