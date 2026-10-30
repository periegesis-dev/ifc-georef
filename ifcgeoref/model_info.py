"""Read everything the workflow needs from an opened IFC file, once.

Runs in worker processes (it needs ifcopenshell); the result is plain JSON so the
web process never has to parse IFC files itself.
"""
import math
import re
from collections import Counter

import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.util.unit

META_VERSION = 4

UNIT_SYMBOLS = {
    "METRE": "m", "MILLIMETRE": "mm", "CENTIMETRE": "cm", "DECIMETRE": "dm", "KILOMETRE": "km",
    "FOOT": "ft", "FEET": "ft", "INCH": "in", "YARD": "yd", "MILE": "mi",
    "US SURVEY FOOT": "ftUS", "SURVEY FOOT": "ftUS",
}
_SYMBOL_BY_SCALE = [(1.0, "m"), (0.001, "mm"), (0.01, "cm"), (0.1, "dm"), (1000.0, "km"),
                    (0.3048, "ft"), (0.0254, "in"), (0.3048006096, "ftUS"), (0.9144, "yd")]
_EPSG = re.compile(r"EPSG\D{0,3}(\d{4,6})", re.IGNORECASE)
MAP_CONVERSION_PSETS = ("epset_mapconversion",)
PROJECTED_CRS_PSETS = ("epset_projectedcrs",)


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def decimal_degrees(compound):
    """IfcCompoundPlaneAngleMeasure (deg, min, sec, millionths) -> decimal degrees."""
    if not compound:
        return None
    parts = [float(v) for v in list(compound)[:4]] + [0.0] * (4 - min(len(compound), 4))
    return parts[0] + parts[1] / 60 + parts[2] / 3600 + parts[3] / 3.6e9


def epsg_from_name(name):
    if not name:
        return None
    m = _EPSG.search(str(name))
    return int(m.group(1)) if m else None


def length_unit(ifc_file):
    unit = None
    try:
        unit = ifcopenshell.util.unit.get_project_unit(ifc_file, "LENGTHUNIT")
    except Exception:
        unit = None
    metres = 1.0
    if unit is not None:
        try:
            metres = float(ifcopenshell.util.unit.calculate_unit_scale(ifc_file))
        except Exception:
            metres = 1.0
    name = None
    if unit is not None:
        if unit.is_a("IfcSIUnit"):
            name = f"{unit.Prefix or ''}{unit.Name}"
        else:
            name = str(getattr(unit, "Name", "") or "").strip().upper() or None
    symbol = UNIT_SYMBOLS.get(name or "")
    if symbol is None:
        symbol = next((s for f, s in _SYMBOL_BY_SCALE if abs(f - metres) < 1e-9), name or "units")
    return {
        "name": name or "METRE",
        "symbol": symbol,
        "metres": metres,
        "assumed": unit is None,
    }


def _property_sets(element):
    result = {}
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pdef = rel.RelatingPropertyDefinition
        if pdef is None or not pdef.is_a("IfcPropertySet"):
            continue
        props = {}
        for prop in pdef.HasProperties or []:
            if prop.is_a("IfcPropertySingleValue"):
                value = prop.NominalValue.wrappedValue if prop.NominalValue is not None else None
                props[prop.Name] = {"id": prop.id(), "value": value}
        result[(pdef.Name or "").strip()] = {
            "id": pdef.id(),
            "name": pdef.Name,
            "guid": pdef.GlobalId,
            "owner_history_id": pdef.OwnerHistory.id() if getattr(pdef, "OwnerHistory", None) else None,
            "property_ids": [p.id() for p in pdef.HasProperties or []],
            "properties": props,
        }
    return result


def _find_pset(psets, names):
    for key, value in psets.items():
        if key.lower() in names:
            return value
    return None


def georef_info(ifc_file, site=None):
    """Existing georeferencing (IfcMapConversion for IFC4+, ePSet_* property sets for IFC2X3)."""
    info = {"present": False}
    projects = ifc_file.by_type("IfcProject")
    if ifc_file.schema != "IFC2X3" and projects:
        conversions = []
        for ctx in projects[0].RepresentationContexts or []:
            for op in getattr(ctx, "HasCoordinateOperation", None) or []:
                if op.is_a("IfcMapConversion"):
                    conversions.append(op)
        if conversions:
            op = conversions[0]
            crs = op.TargetCRS
            name = getattr(crs, "Name", None) if crs else None
            info = {
                "present": True,
                "method": "IfcMapConversion",
                "count": len(conversions),
                "map_conversion_id": op.id(),
                "map_conversion_type": op.is_a(),
                "source_id": op.SourceCRS.id() if op.SourceCRS else None,
                "crs_id": crs.id() if crs else None,
                "crs_type": crs.is_a() if crs else None,
                "crs_name": name,
                "crs_description": getattr(crs, "Description", None) if crs else None,
                "epsg": epsg_from_name(name),
                "eastings": _num(op.Eastings),
                "northings": _num(op.Northings),
                "orthogonal_height": _num(op.OrthogonalHeight) or 0.0,
                "x_axis_abscissa": _num(op.XAxisAbscissa) if op.XAxisAbscissa is not None else 1.0,
                "x_axis_ordinate": _num(op.XAxisOrdinate) if op.XAxisOrdinate is not None else 0.0,
                "scale": _num(op.Scale) if op.Scale is not None else 1.0,
            }
    if ifc_file.schema == "IFC2X3":
        site = site or next(iter(ifc_file.by_type("IfcSite")), None)
        if site is not None:
            psets = _property_sets(site)
            mc = _find_pset(psets, MAP_CONVERSION_PSETS)
            pc = _find_pset(psets, PROJECTED_CRS_PSETS)
            info["psets"] = {"map_conversion": mc, "projected_crs": pc}
            if mc:
                values = {k: v["value"] for k, v in mc["properties"].items()}
                crs_name = (pc or {}).get("properties", {}).get("Name", {}).get("value") \
                    or values.get("TargetCRS")
                if _num(values.get("Eastings")) is not None and _num(values.get("Northings")) is not None:
                    info.update({
                        "present": True,
                        "method": "ePSet_MapConversion",
                        "count": 1,
                        "crs_name": crs_name,
                        "epsg": epsg_from_name(crs_name),
                        "eastings": _num(values.get("Eastings")),
                        "northings": _num(values.get("Northings")),
                        "orthogonal_height": _num(values.get("OrthogonalHeight")) or 0.0,
                        "x_axis_abscissa": _num(values.get("XAxisAbscissa")) if values.get("XAxisAbscissa") is not None else 1.0,
                        "x_axis_ordinate": _num(values.get("XAxisOrdinate")) if values.get("XAxisOrdinate") is not None else 0.0,
                        "scale": _num(values.get("Scale")) if values.get("Scale") is not None else 1.0,
                    })
    if info.get("present"):
        info["rotation_deg"] = math.degrees(math.atan2(info["x_axis_ordinate"], info["x_axis_abscissa"]))
    return info


def _header(ifc_file):
    out = {}
    try:
        fn = ifc_file.header.file_name
        out["name"] = fn.name
        out["timestamp"] = fn.time_stamp
        out["application"] = fn.originating_system or fn.preprocessor_version
        authors = [a for a in (fn.author or []) if a]
        orgs = [o for o in (fn.organization or []) if o]
        out["author"] = ", ".join(authors) or None
        out["organization"] = ", ".join(orgs) or None
    except Exception:
        pass
    return out


def _model_context(project):
    contexts = [c for c in (project.RepresentationContexts or [])
                if c.is_a("IfcGeometricRepresentationContext")
                and not c.is_a("IfcGeometricRepresentationSubContext")]
    model = next((c for c in contexts if (c.ContextType or "").lower() == "model"), None)
    model = model or (contexts[0] if contexts else None)
    if model is None:
        return None
    north = None
    if model.TrueNorth is not None:
        ratios = [float(v) for v in model.TrueNorth.DirectionRatios[:2]]
        length = math.hypot(*ratios) if len(ratios) == 2 else 0
        if length > 1e-12:
            north = [ratios[0] / length, ratios[1] / length]
    wcs = None
    try:
        wcs = [float(v) for v in model.WorldCoordinateSystem.Location.Coordinates]
    except Exception:
        pass
    return {"id": model.id(), "true_north": north, "wcs_origin": wcs}


def _placement_origin(product):
    try:
        if product.ObjectPlacement is None:
            return None
        m = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
        return [float(m[0][3]), float(m[1][3]), float(m[2][3])]
    except Exception:
        return None


def extract(ifc_file):
    """Return a JSON-serialisable description of the model."""
    warnings = []
    schema = ifc_file.schema
    # Highest entity id, without wrapping every entity in Python (seconds and GBs on big models).
    max_id = int(ifc_file.wrapped_data.getMaxId())

    unit = length_unit(ifc_file)
    if unit["assumed"]:
        warnings.append("The file has no length unit. Metres are assumed.")

    projects = ifc_file.by_type("IfcProject")
    project = projects[0] if projects else None
    context = _model_context(project) if project else None
    if context is None:
        warnings.append("The file has no geometric representation context.")

    sites = ifc_file.by_type("IfcSite")
    site_entity = sites[0] if sites else None
    if len(sites) > 1:
        warnings.append(f"The file has {len(sites)} sites; the first one is used.")
    site = None
    if site_entity is not None:
        lat = decimal_degrees(site_entity.RefLatitude)
        lon = decimal_degrees(site_entity.RefLongitude)
        elevation = _num(site_entity.RefElevation)
        has_location = (lat is not None and lon is not None and -90 <= lat <= 90
                        and -180 <= lon <= 180 and not (abs(lat) < 1e-9 and abs(lon) < 1e-9))
        if lat is not None and lon is not None and not has_location:
            warnings.append("The site location in the file is 0°, 0°, which means it was never set.")
        site = {
            "id": site_entity.id(),
            "name": site_entity.Name,
            "latitude": lat if has_location else None,
            "longitude": lon if has_location else None,
            "has_location": has_location,
            "elevation": elevation,
            "elevation_m": None if elevation is None else elevation * unit["metres"],
            "origin": _placement_origin(site_entity) or [0.0, 0.0, 0.0],
            "owner_history_id": site_entity.OwnerHistory.id() if getattr(site_entity, "OwnerHistory", None) else None,
        }

    owner_history_id = (site or {}).get("owner_history_id")
    if owner_history_id is None:
        histories = ifc_file.by_type("IfcOwnerHistory")
        owner_history_id = histories[0].id() if histories else None

    products = ifc_file.by_type("IfcProduct")
    types = Counter(p.is_a() for p in products)
    spatial = {"IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace", "IfcFacility",
               "IfcFacilityPart", "IfcOpeningElement", "IfcAnnotation", "IfcGrid", "IfcVirtualElement"}
    element_count = sum(n for t, n in types.items() if t not in spatial)

    storeys = []
    for storey in ifc_file.by_type("IfcBuildingStorey"):
        elev = _num(getattr(storey, "Elevation", None))
        storeys.append({"name": storey.Name, "elevation_m": None if elev is None else elev * unit["metres"]})
    storeys.sort(key=lambda s: (s["elevation_m"] is None, s["elevation_m"] or 0))

    origins = [o for o in (_placement_origin(p) for p in products if p.Representation) if o]
    placement_bounds = None
    if origins:
        xs, ys, zs = zip(*origins)
        placement_bounds = {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)],
                            "center": [sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)],
                            "count": len(origins)}

    georef = georef_info(ifc_file, site_entity)
    supported = schema == "IFC2X3" or schema.startswith("IFC4")
    if not supported:
        warnings.append(f"{schema} is not supported. Use IFC2X3, IFC4 or IFC4X3.")
    if schema == "IFC2X3" and site_entity is None:
        warnings.append("IFC2X3 files need an IfcSite to store georeferencing.")

    return {
        "version": META_VERSION,
        "schema": schema,
        "supported": supported and not (schema == "IFC2X3" and site_entity is None) and context is not None,
        "max_id": max_id,
        "header": _header(ifc_file),
        "project": {
            "name": project.Name if project else None,
            "description": project.Description if project else None,
        },
        "buildings": [b.Name for b in ifc_file.by_type("IfcBuilding")],
        "storeys": storeys,
        "unit": unit,
        "context": context,
        "site": site,
        "owner_history_id": owner_history_id,
        "element_count": element_count,
        "product_count": len(products),
        "element_types": types.most_common(12),
        "spatial_counts": {
            "projects": len(projects), "sites": len(sites),
            "buildings": len(ifc_file.by_type("IfcBuilding")),
            "building_storeys": len(storeys), "spaces": len(ifc_file.by_type("IfcSpace")),
        },
        "placement_bounds": placement_bounds,
        "georef": georef,
        "warnings": warnings,
    }
