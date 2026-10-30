"""Tessellate an IFC model into a compact binary glTF (for the browser) and a 2D footprint.

Geometry is produced with ifcopenshell's multi-threaded iterator, merged into one
primitive per colour, centred on the model (float32 precision) and converted to
glTF's Y-up axes:  glTF (x, y, z) = (X - ox, Z - oz, -(Y - oy)), where X, Y, Z are
IFC world coordinates in metres and o is "offset_m" in geometry.json.
"""
import gc
import gzip
import json
import struct
import time

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import shapely

SKIP_TYPES = ("IfcSpace", "IfcOpeningElement", "IfcVirtualElement")
DEFAULT_RGBA = (0.82, 0.83, 0.85, 1.0)


def _excluded(ifc_file):
    out = []
    for ifc_type in SKIP_TYPES:
        try:
            out.extend(ifc_file.by_type(ifc_type))
        except RuntimeError:  # type not in this schema
            pass
    return out


def _rgba(material):
    if material is None:
        return DEFAULT_RGBA
    try:
        diffuse = material.diffuse
        rgb = getattr(diffuse, "components", diffuse)
        r, g, b = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
    except Exception:
        r, g, b = DEFAULT_RGBA[:3]
    alpha = 1.0
    try:
        t = float(material.transparency)
        if 0.0 <= t <= 1.0:
            alpha = 1.0 - t
    except Exception:
        pass
    q = lambda v: round(min(max(v, 0.0), 1.0) * 255) / 255
    return (q(r), q(g), q(b), max(q(alpha), 0.15))


class _Bucket:
    __slots__ = ("verts", "faces", "count")

    def __init__(self):
        self.verts, self.faces, self.count = [], [], 0

    def add(self, verts, faces):
        self.verts.append(verts)
        self.faces.append(faces + self.count)
        self.count += len(verts)


def tessellate(ifc_file, threads=2, progress=None):
    """Return (buckets by colour, stats). Coordinates are world metres."""
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    excluded = _excluded(ifc_file)
    iterator = ifcopenshell.geom.iterator(settings, ifc_file, max(1, int(threads)),
                                          exclude=excluded or None)
    buckets = {}
    if not iterator.initialize():
        return buckets, {"shapes": 0, "triangles": 0}
    # Worker processes are short-lived; skipping cyclic GC passes speeds up this allocation-heavy loop.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        stats = _collect(iterator, buckets, progress)
    finally:
        if gc_was_enabled:
            gc.enable()
    return buckets, stats


def _collect(iterator, buckets, progress):
    shapes, triangles = 0, 0
    last = 0.0
    while True:
        shape = iterator.get()
        geom = shape.geometry
        faces = np.asarray(geom.faces, dtype=np.int64).reshape(-1, 3)
        if len(faces):
            verts = np.asarray(geom.verts, dtype=np.float64).reshape(-1, 3)
            materials = list(geom.materials)
            mat_ids = np.asarray(geom.material_ids, dtype=np.int64)
            if len(mat_ids) != len(faces):
                mat_ids = np.full(len(faces), -1, dtype=np.int64)
            for mid in np.unique(mat_ids):
                sel = faces[mat_ids == mid]
                used, remapped = np.unique(sel.ravel(), return_inverse=True)
                material = materials[mid] if 0 <= mid < len(materials) else None
                key = _rgba(material)
                buckets.setdefault(key, _Bucket()).add(verts[used], remapped.reshape(-1, 3))
            shapes += 1
            triangles += len(faces)
        now = time.monotonic()
        if progress and now - last > 0.3:
            last = now
            progress(iterator.progress() / 100.0, f"{shapes:,} elements")
        if not iterator.next():
            break
    return {"shapes": shapes, "triangles": triangles}


def _edge_samples(xy, faces, step=0.5, cap=200):
    """Points every `step` metres along triangle edges, so long walls are not cut by the hull."""
    a = xy[faces].reshape(-1, 2)
    b = xy[np.roll(faces, -1, axis=1)].reshape(-1, 2)
    n = np.minimum(np.ceil(np.hypot(*(b - a).T) / step), cap).astype(np.int64)
    keep = n > 1
    a, b, n = a[keep], b[keep], n[keep]
    if not len(n):
        return np.empty((0, 2))
    edge = np.repeat(np.arange(len(n)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    t = (k / np.repeat(n, n))[:, None]
    return a[edge] + (b[edge] - a[edge]) * t


def _footprint(xy):
    """Outline of the model seen from above (local metres)."""
    cell = 0.05
    points = np.unique(np.round(xy / cell).astype(np.int64), axis=0) * cell
    while len(points) > 40000:  # thin on a coarser grid; keeps the boundary evenly sampled
        cell *= 2
        points = np.unique(np.round(points / cell).astype(np.int64), axis=0) * cell
    cloud = shapely.multipoints(points)
    convex = cloud.convex_hull
    outline, kind = convex, "convex"
    if len(points) > 3:
        try:
            concave = shapely.concave_hull(cloud, ratio=0.1)
            if concave.geom_type == "Polygon" and concave.is_valid and concave.area > 0.5 * convex.area:
                outline, kind = concave, "concave"
        except Exception:
            pass
    if outline.geom_type != "Polygon":
        outline = outline.buffer(0.5)
        kind = "convex"
    outline = outline.simplify(max(0.05, cell), preserve_topology=True)
    coords = [[round(x, 3), round(y, 3)] for x, y in list(outline.exterior.coords)[:-1]]
    return coords, kind


def _glb(primitives):
    buffer = bytearray()
    views, accessors, materials, prims = [], [], [], []
    for i, (rgba, positions, indices) in enumerate(primitives):
        pos = positions.astype("<f4")
        idx = indices.astype("<u4")
        views.append({"buffer": 0, "byteOffset": len(buffer), "byteLength": pos.nbytes, "target": 34962})
        buffer += pos.tobytes()
        views.append({"buffer": 0, "byteOffset": len(buffer), "byteLength": idx.nbytes, "target": 34963})
        buffer += idx.tobytes()
        accessors.append({"bufferView": 2 * i, "componentType": 5126, "count": len(pos), "type": "VEC3",
                          "min": pos.min(axis=0).tolist(), "max": pos.max(axis=0).tolist()})
        accessors.append({"bufferView": 2 * i + 1, "componentType": 5125, "count": len(idx), "type": "SCALAR"})
        material = {"pbrMetallicRoughness": {"baseColorFactor": list(rgba), "metallicFactor": 0.0,
                                             "roughnessFactor": 0.9}, "doubleSided": True}
        if rgba[3] < 0.999:
            material["alphaMode"] = "BLEND"
        materials.append(material)
        prims.append({"attributes": {"POSITION": 2 * i}, "indices": 2 * i + 1, "material": i})
    doc = {
        "asset": {"version": "2.0", "generator": "ifc-georef"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "model"}],
        "meshes": [{"primitives": prims}], "materials": materials,
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(buffer)}],
    }
    json_chunk = json.dumps(doc, separators=(",", ":")).encode()
    json_chunk += b" " * (-len(json_chunk) % 4)
    buffer += b"\0" * (-len(buffer) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(buffer)
    return b"".join([
        struct.pack("<III", 0x46546C67, 2, total),
        struct.pack("<II", len(json_chunk), 0x4E4F534A), json_chunk,
        struct.pack("<II", len(buffer), 0x004E4942), bytes(buffer),
    ])


def build(ifc_file, glb_gz_path, threads=2, progress=None, package_progress=None):
    """Tessellate and write model.glb.gz. Returns the geometry.json content."""
    buckets, stats = tessellate(ifc_file, threads, progress)
    if not buckets:
        return {"empty": True, **stats}
    if package_progress:
        package_progress(0.1, "Merging meshes")
    merged = []
    for rgba, bucket in buckets.items():
        merged.append((rgba, np.concatenate(bucket.verts), np.concatenate(bucket.faces).ravel()))
    all_verts = np.concatenate([m[1] for m in merged])
    vmin, vmax = all_verts.min(axis=0), all_verts.max(axis=0)
    offset = np.round((vmin + vmax) / 2.0, 0)
    if package_progress:
        package_progress(0.3, "Computing footprint")
    xy = [all_verts[:, :2]] + [_edge_samples(v[:, :2], f.reshape(-1, 3)) for _, v, f in merged]
    footprint, kind = _footprint(np.concatenate(xy))
    del all_verts, xy

    if package_progress:
        package_progress(0.6, "Writing 3D model")
    primitives = []
    for rgba, verts, faces in merged:
        local = verts - offset
        gl = np.column_stack([local[:, 0], local[:, 2], -local[:, 1]])
        primitives.append((rgba, gl, faces))
    data = _glb(primitives)
    with open(glb_gz_path, "wb") as fh:
        fh.write(gzip.compress(data, compresslevel=5))
    return {
        "empty": False,
        "bounds_m": {"min": vmin.tolist(), "max": vmax.tolist()},
        "size_m": (vmax - vmin).tolist(),
        "offset_m": offset.tolist(),
        "footprint_m": footprint,
        "footprint_kind": kind,
        "shapes": stats["shapes"],
        "triangles": stats["triangles"],
        "glb_bytes": len(data),
        "colours": len(primitives),
    }
