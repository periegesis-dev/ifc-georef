"""Write georeferencing into an IFC file.

IFC4 / IFC4X3: IfcProjectedCRS (with MapUnit) + IfcMapConversion attached to the model
               context. An existing IfcMapConversionScaled keeps its type and factors.
IFC2X3:        ePSet_MapConversion + ePSet_ProjectedCRS property sets on the IfcSite
               (buildingSMART georeferencing guide convention).
All schemas:   GDH_Georeferencing on IfcProject — the tool's own placement data
               (control points, scale/rotation source) as JSON, so the file can be
               re-edited exactly. Deliberately not a Pset_/ePSet_ name.

Existing records are replaced in place; everything else in the file is copied unchanged.
"""
import json
import os
import time
from pathlib import Path

import ifcopenshell.guid

from . import step
from .model_info import GDH_PSET

CHUNK = 8 * 1024 * 1024


class _Ids:
    def __init__(self, max_id):
        self.next = int(max_id) + 1

    def __call__(self):
        value = self.next
        self.next += 1
        return value


def _map_unit(meta, crs, ids, append):
    """Reference to the CRS length unit (reusing the file's METRE unit when possible)."""
    factor = crs.get("unit_metres") or 1.0
    metre = meta.get("metre_unit_id")
    if metre is None:
        metre = ids()
        append.append(step.record(metre, "IfcSIUnit", ["*", ".LENGTHUNIT.", "$", ".METRE."]))
    if abs(factor - 1.0) < 1e-12:
        return metre
    dims, measure, unit = ids(), ids(), ids()
    name = (crs.get("unit") or "FOOT").upper()
    append += [
        step.record(dims, "IfcDimensionalExponents", ["1", "0", "0", "0", "0", "0", "0"]),
        step.record(measure, "IfcMeasureWithUnit", [f"IFCLENGTHMEASURE({step.real(factor)})", step.ref(metre)]),
        step.record(unit, "IfcConversionBasedUnit", [step.ref(dims), ".LENGTHUNIT.", step.string(name), step.ref(measure)]),
    ]
    return unit


def _crs_fields(crs, map_unit_id):
    return [
        step.string(crs["id"]),
        step.optional_string(crs.get("name")),
        step.optional_string(crs.get("datum"), 255),
        "$",
        step.optional_string(crs.get("projection"), 255),
        step.optional_string(crs.get("zone"), 255),
        step.ref(map_unit_id) if map_unit_id else "$",
    ]


def _ifc4_records(meta, sol, crs, ids, replace, append):
    existing = meta.get("georef") or {}
    context_id = existing.get("source_id") or (meta.get("context") or {}).get("id")
    if context_id is None:
        raise ValueError("The file has no geometric representation context to georeference.")

    map_unit = _map_unit(meta, crs, ids, append)
    if existing.get("present") and existing.get("crs_id") and existing.get("crs_type") == "IfcProjectedCRS":
        crs_id = existing["crs_id"]
        replace[crs_id] = step.record(crs_id, "IfcProjectedCRS", _crs_fields(crs, map_unit))
    else:
        crs_id = ids()
        append.append(step.record(crs_id, "IfcProjectedCRS", _crs_fields(crs, map_unit)))

    args = [
        step.ref(context_id), step.ref(crs_id),
        step.real(sol["eastings"]), step.real(sol["northings"]), step.real(sol["orthogonal_height"]),
        step.real(sol["x_axis_abscissa"]), step.real(sol["x_axis_ordinate"]), step.real(sol["scale"]),
    ]
    entity = "IfcMapConversion"
    if existing.get("map_conversion_type") == "IfcMapConversionScaled":
        entity = "IfcMapConversionScaled"
        args += [step.real(f) for f in (existing.get("factors") or [1.0, 1.0, 1.0])]
    if existing.get("present") and existing.get("map_conversion_id"):
        mc_id = existing["map_conversion_id"]
        replace[mc_id] = step.record(mc_id, entity, args)
    else:
        append.append(step.record(ids(), entity, args))


def _owner_history(ids, append):
    person, org, pao, app, oh = ids(), ids(), ids(), ids(), ids()
    append += [
        step.record(person, "IfcPerson", ["$", step.string("IFC Georeferencing"), "$", "$", "$", "$", "$", "$"]),
        step.record(org, "IfcOrganization", ["$", step.string("IFC Georeferencing"), "$", "$", "$"]),
        step.record(pao, "IfcPersonAndOrganization", [step.ref(person), step.ref(org), "$"]),
        step.record(app, "IfcApplication", [step.ref(org), step.string("1.0"),
                                            step.string("IFC Georeferencing"), step.string("ifc-georef")]),
        step.record(oh, "IfcOwnerHistory", [step.ref(pao), step.ref(app), "$", ".ADDED.", "$", "$", "$",
                                            str(int(time.time()))]),
    ]
    return oh


def _pset_records(name, values, existing, element_id, owner_history_id, ids, replace, append, description=None):
    """values: list of (property name, IFC type, encoded value) — None values are skipped."""
    existing_props = (existing or {}).get("properties", {})
    prop_ids = []
    for prop_name, ifc_type, encoded in values:
        if encoded is None:
            continue
        rec_args = [step.string(prop_name), "$", f"{ifc_type}({encoded})", "$"]
        if prop_name in existing_props:
            pid = existing_props[prop_name]["id"]
            replace[pid] = step.record(pid, "IfcPropertySingleValue", rec_args)
        else:
            pid = ids()
            append.append(step.record(pid, "IfcPropertySingleValue", rec_args))
        prop_ids.append(pid)

    owner = step.ref(owner_history_id) if owner_history_id else "$"
    if existing:
        # keep any other properties the set already had
        keep = [pid for pid in existing.get("property_ids", []) if pid not in prop_ids]
        all_ids = keep + prop_ids
        own = existing.get("owner_history_id")
        replace[existing["id"]] = step.record(existing["id"], "IfcPropertySet", [
            step.string(existing["guid"]), step.ref(own) if own else owner,
            step.string(existing.get("name") or name), step.optional_string(description),
            "(" + ",".join(step.ref(i) for i in all_ids) + ")",
        ])
        return
    pset_id = ids()
    append.append(step.record(pset_id, "IfcPropertySet", [
        step.string(ifcopenshell.guid.new()), owner, step.string(name), step.optional_string(description),
        "(" + ",".join(step.ref(i) for i in prop_ids) + ")",
    ]))
    append.append(step.record(ids(), "IfcRelDefinesByProperties", [
        step.string(ifcopenshell.guid.new()), owner, "$", "$",
        f"({step.ref(element_id)})", step.ref(pset_id),
    ]))


def _ifc2x3_records(meta, sol, crs, ids, replace, append, owner_history_id):
    site = meta.get("site")
    if not site:
        raise ValueError("IFC2X3 files need an IfcSite to store georeferencing.")
    psets = (meta.get("georef") or {}).get("psets") or {}
    epsg = step.string(crs["id"])

    _pset_records("ePSet_MapConversion", [
        ("TargetCRS", "IFCLABEL", epsg),
        ("Eastings", "IFCLENGTHMEASURE", step.real(sol["eastings"])),
        ("Northings", "IFCLENGTHMEASURE", step.real(sol["northings"])),
        ("OrthogonalHeight", "IFCLENGTHMEASURE", step.real(sol["orthogonal_height"])),
        ("XAxisAbscissa", "IFCREAL", step.real(sol["x_axis_abscissa"])),
        ("XAxisOrdinate", "IFCREAL", step.real(sol["x_axis_ordinate"])),
        ("Scale", "IFCREAL", step.real(sol["scale"])),
    ], psets.get("map_conversion"), site["id"], owner_history_id, ids, replace, append)

    def opt(value, limit=255):
        return None if not value else step.string(str(value)[:limit])

    _pset_records("ePSet_ProjectedCRS", [
        ("Name", "IFCLABEL", epsg),
        ("Description", "IFCTEXT", opt(crs.get("name"))),
        ("GeodeticDatum", "IFCLABEL", opt(crs.get("datum"))),
        ("MapProjection", "IFCLABEL", opt(crs.get("projection"))),
        ("MapZone", "IFCLABEL", opt(crs.get("zone"))),
        ("MapUnit", "IFCLABEL", opt((crs.get("unit") or "metre").upper())),
    ], psets.get("projected_crs"), site["id"], owner_history_id, ids, replace, append)


def _gdh_records(meta, gdh, ids, replace, append, owner_history_id):
    project_id = meta.get("project_id")
    if not gdh or project_id is None:
        return
    text = step.string(json.dumps(gdh, separators=(",", ":"), sort_keys=True))
    gdh_ids = meta.get("gdh_ids") or {}
    if gdh_ids.get("property_id"):
        pid = gdh_ids["property_id"]
        replace[pid] = step.record(pid, "IfcPropertySingleValue", [step.string("Data"), "$", f"IFCTEXT({text})", "$"])
        return
    existing = None
    if gdh_ids.get("pset_id"):
        existing = {"id": gdh_ids["pset_id"], "guid": gdh_ids["guid"], "name": GDH_PSET, "properties": {},
                    "property_ids": gdh_ids.get("property_ids") or [],
                    "owner_history_id": gdh_ids.get("owner_history_id")}
    _pset_records(GDH_PSET, [("Data", "IFCTEXT", text)], existing, project_id, owner_history_id, ids, replace,
                  append, description="Placement data of the IFC Georeferencing tool (GeoDataHub); "
                                      "not part of any IFC standard property set.")


def plan(meta, solution, crs, gdh=None):
    """Records to replace ({id: text}) and to append ([text])."""
    ids = _Ids(meta["max_id"])
    replace, append = {}, []
    owner_history_id = meta.get("owner_history_id")
    if meta["schema"] == "IFC2X3":
        owner_history_id = owner_history_id or _owner_history(ids, append)
        _ifc2x3_records(meta, solution, crs, ids, replace, append, owner_history_id)
    else:
        _ifc4_records(meta, solution, crs, ids, replace, append)
    _gdh_records(meta, gdh, ids, replace, append, owner_history_id)
    return replace, append


def write(src, dst, meta, solution, crs, progress=None, gdh=None):
    """Copy src to dst with georeferencing added. Returns a short summary."""
    data = Path(src).read_bytes()
    nl = step.newline(data)
    replace, append = plan(meta, solution, crs, gdh)

    edits = []
    for entity_id, text in replace.items():
        span = step.record_span(data, entity_id)
        if span is None:
            raise ValueError(f"Entity #{entity_id} was not found in the file.")
        edits.append((span[0], span[1], text.encode("ascii")))
    insert_at = step.data_section_end(data)
    block = b""
    if append:
        prefix = b"" if insert_at == 0 or data[insert_at - 1:insert_at] == b"\n" else nl
        block = prefix + nl.join(t.encode("ascii") for t in append) + nl
    edits.append((insert_at, insert_at, block))
    edits.sort(key=lambda e: e[0])

    total = len(data) + sum(len(e[2]) - (e[1] - e[0]) for e in edits)
    written = 0
    view = memoryview(data)
    dst = Path(dst)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with open(tmp, "wb") as out:
        pos = 0
        for start, end, text in edits:
            for i in range(pos, start, CHUNK):
                chunk = view[i:min(i + CHUNK, start)]
                out.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written / total)
            out.write(text)
            written += len(text)
            pos = end
        for i in range(pos, len(data), CHUNK):
            chunk = view[i:min(i + CHUNK, len(data))]
            out.write(chunk)
            written += len(chunk)
            if progress:
                progress(written / total)
    os.replace(tmp, dst)
    if progress:
        progress(1.0)
    return {"bytes": total, "replaced": len(replace), "added": len(append)}
