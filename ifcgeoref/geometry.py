"""Tessellate an IFC model into a compact binary glTF, snapping edges and a 2D footprint.

Geometry is produced with ifcopenshell's multi-threaded iterator, merged into one
primitive per colour, centred on the model (float32 precision) and converted to
glTF's Y-up axes:  glTF (x, y, z) = (X - ox, Z - oz, -(Y - oy)), where X, Y, Z are
IFC world coordinates in metres and o is "offset_m" in geometry.json.

edges.bin.gz holds the model's *feature edges* (where faces meet at more than
FEATURE_ANGLE degrees, plus open boundaries) as float32 segments in the same glTF
frame — the snapping targets of the 3D picker. Triangulation diagonals inside flat
faces are not feature edges, so they are never offered for snapping.
"""
import gc
import gzip
import json
import re
import struct
import time

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import shapely

SKIP_TYPES = ("IfcSpace", "IfcOpeningElement", "IfcVirtualElement")
# Site / terrain geometry is drawn but left out of the default footprint.
CONTEXT_TYPES = {"IfcSite", "IfcGeographicElement"}
CONTEXT_NAME = re.compile(r"terrain|topograph|gel(ä|ae)nde|ground ?surface|toposolid", re.IGNORECASE)
DEFAULT_RGBA = (0.82, 0.83, 0.85, 1.0)
FEATURE_ANGLE = 20.0
WELD = 1e-4          # metres; vertices closer than this are the same point
MAX_EDGES = 2_500_000


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


def is_context(ifc_type, name):
    return ifc_type in CONTEXT_TYPES or bool(name and CONTEXT_NAME.search(str(name)))


def feature_edges(verts, faces, angle_deg=FEATURE_ANGLE):
    """Segments (a, b) of edges where faces meet at more than angle_deg, or open edges."""
    if not len(faces):
        return np.empty((0, 3)), np.empty((0, 3))
    keys, inverse = np.unique(np.round(verts / WELD).astype(np.int64), axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    pos = keys * WELD
    f = inverse[faces]
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    if not len(f):
        return np.empty((0, 3)), np.empty((0, 3))
    a, b, c = pos[f[:, 0]], pos[f[:, 1]], pos[f[:, 2]]
    normal = np.cross(b - a, c - a)
    length = np.linalg.norm(normal, axis=1)
    keep = length > 1e-12
    f, normal = f[keep], normal[keep] / length[keep, None]
    if not len(f):
        return np.empty((0, 3)), np.empty((0, 3))
    edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    face_of = np.tile(np.arange(len(f)), 3)
    edges.sort(axis=1)
    key = edges[:, 0] * (len(pos) + 1) + edges[:, 1]
    order = np.argsort(key, kind="stable")
    _, start, counts = np.unique(key[order], return_index=True, return_counts=True)
    feature = counts != 2  # open (1 face) or non-manifold (3+) edges
    pairs = np.where(counts == 2)[0]
    if len(pairs):
        f1 = face_of[order[start[pairs]]]
        f2 = face_of[order[start[pairs] + 1]]
        cos = np.einsum("ij,ij->i", normal[f1], normal[f2])
        feature[pairs] = cos < np.cos(np.radians(angle_deg))
    chosen = edges[order[start[feature]]]
    return pos[chosen[:, 0]], pos[chosen[:, 1]]


def _edge_samples(a, b, step=0.5, cap=200):
    """Points every `step` metres along segments (keeps long walls in the footprint)."""
    if not len(a):
        return np.empty((0, 2))
    a, b = a[:, :2], b[:, :2]
    n = np.minimum(np.ceil(np.hypot(*(b - a).T) / step), cap).astype(np.int64)
    keep = n > 1
    a, b, n = a[keep], b[keep], n[keep]
    if not len(n):
        return np.empty((0, 2))
    edge = np.repeat(np.arange(len(n)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    t = (k / np.repeat(n, n))[:, None]
    return a[edge] + (b[edge] - a[edge]) * t


class _Bucket:
    __slots__ = ("verts", "faces", "count")

    def __init__(self):
        self.verts, self.faces, self.count = [], [], 0

    def add(self, verts, faces):
        self.verts.append(verts)
        self.faces.append(faces + self.count)
        self.count += len(verts)


class _Collected:
    def __init__(self):
        self.buckets = {}
        self.edges_a, self.edges_b = [], []
        self.edge_count = 0
        self.xy = {"building": [], "context": []}
        self.bounds = {"building": [None, None], "context": [None, None]}
        self.shapes = 0
        self.triangles = 0

    def extend_bounds(self, kind, verts):
        lo, hi = verts.min(axis=0), verts.max(axis=0)
        cur = self.bounds[kind]
        cur[0] = lo if cur[0] is None else np.minimum(cur[0], lo)
        cur[1] = hi if cur[1] is None else np.maximum(cur[1], hi)


def tessellate(ifc_file, threads=2, progress=None, edges=True):
    """Iterate the model's geometry. Coordinates are world metres."""
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    excluded = _excluded(ifc_file)
    iterator = ifcopenshell.geom.iterator(settings, ifc_file, max(1, int(threads)),
                                          exclude=excluded or None)
    out = _Collected()
    if not iterator.initialize():
        return out
    # Worker processes are short-lived; skipping cyclic GC passes speeds up this allocation-heavy loop.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        _collect(iterator, out, progress, edges)
    finally:
        if gc_was_enabled:
            gc.enable()
    return out


def _collect(iterator, out, progress, edges):
    last = 0.0
    while True:
        shape = iterator.get()
        geom = shape.geometry
        faces = np.asarray(geom.faces, dtype=np.int64).reshape(-1, 3)
        if len(faces):
            verts = np.asarray(geom.verts, dtype=np.float64).reshape(-1, 3)
            kind = "context" if is_context(shape.type, getattr(shape, "name", None)) else "building"
            materials = list(geom.materials)
            mat_ids = np.asarray(geom.material_ids, dtype=np.int64)
            if len(mat_ids) != len(faces):
                mat_ids = np.full(len(faces), -1, dtype=np.int64)
            for mid in np.unique(mat_ids):
                sel = faces[mat_ids == mid]
                used, remapped = np.unique(sel.ravel(), return_inverse=True)
                material = materials[mid] if 0 <= mid < len(materials) else None
                out.buckets.setdefault(_rgba(material), _Bucket()).add(verts[used], remapped.reshape(-1, 3))
            a, b = feature_edges(verts, faces)
            if edges and out.edge_count < MAX_EDGES and len(a):
                out.edges_a.append(a)
                out.edges_b.append(b)
                out.edge_count += len(a)
            out.xy[kind].append(verts[:, :2])
            out.xy[kind].append(_edge_samples(a, b))
            out.extend_bounds(kind, verts)
            out.shapes += 1
            out.triangles += len(faces)
        now = time.monotonic()
        if progress and now - last > 0.3:
            last = now
            progress(iterator.progress() / 100.0, f"{out.shapes:,} elements")
        if not iterator.next():
            break


def extents(ifc_file, threads=2):
    """World-metre bounding box of all geometry (min, max) or None, without building a model."""
    collected = tessellate(ifc_file, threads, edges=False)
    return _bounds_all(collected)


def _bounds_all(collected):
    parts = [b for b in collected.bounds.values() if b[0] is not None]
    if not parts:
        return None
    lo = np.min([p[0] for p in parts], axis=0)
    hi = np.max([p[1] for p in parts], axis=0)
    return {"min": lo.tolist(), "max": hi.tolist()}


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


def _to_gl(world, offset):
    local = world - offset
    return np.column_stack([local[:, 0], local[:, 2], -local[:, 1]])


def build(ifc_file, glb_gz_path, threads=2, progress=None, package_progress=None, edges_gz_path=None):
    """Tessellate and write model.glb.gz (+ edges.bin.gz). Returns the geometry.json content."""
    collected = tessellate(ifc_file, threads, progress)
    if not collected.buckets:
        return {"empty": True, "shapes": collected.shapes, "triangles": collected.triangles}
    if package_progress:
        package_progress(0.1, "Merging meshes")
    merged = []
    for rgba, bucket in collected.buckets.items():
        merged.append((rgba, np.concatenate(bucket.verts), np.concatenate(bucket.faces).ravel()))
    bounds = _bounds_all(collected)
    vmin, vmax = np.array(bounds["min"]), np.array(bounds["max"])
    offset = np.round((vmin + vmax) / 2.0, 0)

    if package_progress:
        package_progress(0.3, "Computing footprint")
    building_xy = [x for x in collected.xy["building"] if len(x)]
    context_xy = [x for x in collected.xy["context"] if len(x)]
    footprint, kind = _footprint(np.concatenate(building_xy or context_xy))
    footprint_all, kind_all = footprint, kind
    if building_xy and context_xy:
        footprint_all, kind_all = _footprint(np.concatenate(building_xy + context_xy))

    if package_progress:
        package_progress(0.6, "Writing 3D model")
    data = _glb([(rgba, _to_gl(verts, offset), faces) for rgba, verts, faces in merged])
    with open(glb_gz_path, "wb") as fh:
        fh.write(gzip.compress(data, compresslevel=5))

    edge_count = 0
    if edges_gz_path and collected.edges_a:
        a = _to_gl(np.concatenate(collected.edges_a), offset)
        b = _to_gl(np.concatenate(collected.edges_b), offset)
        segments = np.empty((len(a), 6), dtype="<f4")
        segments[:, :3], segments[:, 3:] = a, b
        edge_count = len(segments)
        with open(edges_gz_path, "wb") as fh:
            fh.write(gzip.compress(segments.tobytes(), compresslevel=5))

    building_bounds = collected.bounds["building"]
    return {
        "empty": False,
        "bounds_m": bounds,
        "bounds_building_m": None if building_bounds[0] is None else
        {"min": building_bounds[0].tolist(), "max": building_bounds[1].tolist()},
        "size_m": (vmax - vmin).tolist(),
        "offset_m": offset.tolist(),
        "footprint_m": footprint,
        "footprint_kind": kind,
        "footprint_all_m": footprint_all,
        "has_context": bool(context_xy),
        "shapes": collected.shapes,
        "triangles": collected.triangles,
        "edges": edge_count,
        "glb_bytes": len(data),
        "colours": len(merged),
    }
