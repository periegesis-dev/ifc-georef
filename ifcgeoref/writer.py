"""Write georeferencing into an IFC file.

IFC4 / IFC4X3: IfcProjectedCRS + IfcMapConversion attached to the model context.
IFC2X3:        ePSet_MapConversion + ePSet_ProjectedCRS property sets on the IfcSite
               (buildingSMART georeferencing guide convention).

Existing georeferencing records are replaced in place; everything else in the
file is copied unchanged.
"""
import os
import time
from pathlib import Path

import ifcopenshell.guid

from . import step

CHUNK = 8 * 1024 * 1024


class _Ids:
    def __init__(self, max_id):
        self.next = int(max_id) + 1

    def __call__(self):
        value = self.next
        self.next += 1
        return value


def _crs_fields(crs):
    return [
        step.string(crs["id"]),
        step.optional_string(crs.get("name")),
        step.optional_string(crs.get("datum"), 255),
        "$",
        step.optional_string(crs.get("projection"), 255),
        step.optional_string(crs.get("zone"), 255),
        "$",
    ]


def _ifc4_records(meta, sol, crs, ids):
    replace, append = {}, []
    existing = meta.get("georef") or {}
    context_id = existing.get("source_id") or (meta.get("context") or {}).get("id")
    if context_id is None:
        raise ValueError("The file has no geometric representation context to georeference.")

    if existing.get("present") and existing.get("crs_id") and existing.get("crs_type") == "IfcProjectedCRS":
        crs_id = existing["crs_id"]
        replace[crs_id] = step.record(crs_id, "IfcProjectedCRS", _crs_fields(crs))
    else:
        crs_id = ids()
        append.append(step.record(crs_id, "IfcProjectedCRS", _crs_fields(crs)))

    args = [
        step.ref(context_id), step.ref(crs_id),
        step.real(sol["eastings"]), step.real(sol["northings"]), step.real(sol["orthogonal_height"]),
        step.real(sol["x_axis_abscissa"]), step.real(sol["x_axis_ordinate"]), step.real(sol["scale"]),
    ]
    if existing.get("present") and existing.get("map_conversion_id"):
        mc_id = existing["map_conversion_id"]
        replace[mc_id] = step.record(mc_id, "IfcMapConversion", args)
    else:
        append.append(step.record(ids(), "IfcMapConversion", args))
    return replace, append


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


def _pset_records(name, values, existing, site_id, owner_history_id, ids, replace, append):
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

    if existing:
        # keep any other properties the set already had
        keep = [pid for pid in existing.get("property_ids", []) if pid not in prop_ids]
        all_ids = keep + prop_ids
        owner = existing.get("owner_history_id") or owner_history_id
        replace[existing["id"]] = step.record(existing["id"], "IfcPropertySet", [
            step.string(existing["guid"]), step.ref(owner) if owner else "$",
            step.string(existing.get("name") or name), "$",
            "(" + ",".join(step.ref(i) for i in all_ids) + ")",
        ])
        return
    pset_id = ids()
    append.append(step.record(pset_id, "IfcPropertySet", [
        step.string(ifcopenshell.guid.new()), step.ref(owner_history_id), step.string(name), "$",
        "(" + ",".join(step.ref(i) for i in prop_ids) + ")",
    ]))
    append.append(step.record(ids(), "IfcRelDefinesByProperties", [
        step.string(ifcopenshell.guid.new()), step.ref(owner_history_id), "$", "$",
        f"({step.ref(site_id)})", step.ref(pset_id),
    ]))


def _ifc2x3_records(meta, sol, crs, ids):
    replace, append = {}, []
    site = meta.get("site")
    if not site:
        raise ValueError("IFC2X3 files need an IfcSite to store georeferencing.")
    psets = (meta.get("georef") or {}).get("psets") or {}
    owner_history_id = meta.get("owner_history_id") or _owner_history(ids, append)
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
    ], psets.get("projected_crs"), site["id"], owner_history_id, ids, replace, append)
    return replace, append


def plan(meta, solution, crs):
    """Records to replace ({id: text}) and to append ([text])."""
    ids = _Ids(meta["max_id"])
    if meta["schema"] == "IFC2X3":
        return _ifc2x3_records(meta, solution, crs, ids)
    return _ifc4_records(meta, solution, crs, ids)


def write(src, dst, meta, solution, crs, progress=None):
    """Copy src to dst with georeferencing added. Returns a short summary."""
    data = Path(src).read_bytes()
    nl = step.newline(data)
    replace, append = plan(meta, solution, crs)

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
