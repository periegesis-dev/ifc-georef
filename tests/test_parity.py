"""Georeferencing parity brief: manual placement, scale, rotation pivot, re-edit, R11 API."""
import io
import math
import zipfile

import ifcopenshell
import ifcopenshell.guid
import pytest

from ifcgeoref import crs, geometry, model_info, solver, writer
from conftest import HOUSE, HOUSE_GEOREF, make_ifc2x3, upload, wait_task


def prepared(client, path, name=None):
    job_id = upload(client, path, name).get_json()["job_id"]
    status, task = wait_task(client, job_id, "prepare")
    assert task["state"] == "done", task.get("error")
    return job_id, status


def solve(client, job_id, payload):
    res = client.post(f"/api/jobs/{job_id}/solve", json=payload)
    assert res.status_code == 200, res.get_json()
    return res.get_json()


# ---- R1: placement with no control points ------------------------------------------
def test_zero_point_placement_is_written_where_shown(client, tmp_path):
    path = make_ifc2x3(tmp_path / "nosite.ifc", location=False)  # no site reference, no points
    job_id, _ = prepared(client, path)
    payload = {"epsg": 2056, "use_site": False, "points": [], "rotation_deg": -30.0, "height": 512.0,
               "anchor": {"local": [10.0, 20.0], "map": [2683000.0, 1248000.0]}}
    result = solve(client, job_id, payload)
    sol = result["solution"]
    assert sol["position_source"] == "manual" and sol["rms_horizontal_m"] is None
    assert sol["rotation_cw_deg"] == pytest.approx(30.0)
    e, n, h = solver.apply(sol, [(10.0, 20.0, 0.0)])[0]
    assert (e, n) == pytest.approx((2683000.0, 1248000.0), abs=1e-6)
    assert h == pytest.approx(512.0)

    assert client.post(f"/api/jobs/{job_id}/georeference", json=payload).status_code == 202
    _, task = wait_task(client, job_id, "georeference")
    assert task["state"] == "done", task.get("error")
    out = tmp_path / "out.ifc"
    out.write_bytes(client.get(f"/api/jobs/{job_id}/download").data)
    info = model_info.georef_info(ifcopenshell.open(str(out)))
    shown = solver.apply(info, [(10.0, 20.0, 0.0)])[0]
    assert shown[:2] == pytest.approx((2683000.0, 1248000.0), abs=1e-6)


def test_hand_placement_far_from_the_site_checks_the_crs_where_placed(client):
    # The house's IfcSite is in London; placed by hand in Bern, LV95 must cover it.
    job_id, _ = prepared(client, HOUSE)
    result = solve(client, job_id, {"epsg": 2056, "use_site": False, "rotation_deg": 0,
                                    "anchor": {"local": [0, 0], "map": [2600000.0, 1200000.0]}})
    assert result["can_save"] and result["covers_location"] is True
    assert result["location"]["source"] == "manual"
    assert result["location"]["lat"] == pytest.approx(46.95, abs=0.01)
    assert not any(w["code"] == "outside_area" for w in result["solution"]["warnings"])


def test_georef_location_comes_from_the_map_conversion(client):
    _, status = prepared(client, HOUSE_GEOREF)
    g = status["meta"]["georef"]
    lon, lat = crs.to_wgs84(32643, [(g["eastings"], g["northings"])])[0]
    assert (g["latitude"], g["longitude"]) == pytest.approx((lat, lon))


def test_no_points_and_no_anchor_is_rejected(client, tmp_path):
    job_id, _ = prepared(client, make_ifc2x3(tmp_path / "a.ifc", location=False))
    res = client.post(f"/api/jobs/{job_id}/solve", json={"epsg": 2056})
    assert res.status_code == 422 and "by hand" in res.get_json()["error"]["message"]


# ---- R5: rotation pivots on the footprint centre ------------------------------------
def test_manual_rotation_keeps_the_footprint_centre(client):
    job_id, _ = prepared(client, HOUSE)
    base = solve(client, job_id, {"epsg": 27700, "use_site": True})
    pivot_local, pivot_map = base["client"]["pivot_local"], base["client"]["pivot_map"]
    rotated = solve(client, job_id, {"epsg": 27700, "use_site": True, "rotation_deg": -30.0,
                                     "anchor": {"local": pivot_local, "map": pivot_map}})
    sol = rotated["solution"]
    assert sol["rotation_cw_deg"] == pytest.approx(30.0)
    centre = solver.apply(sol, [(pivot_local[0], pivot_local[1], 0.0)])[0]
    assert centre[:2] == pytest.approx(pivot_map, abs=0.001)  # within 1 mm


def test_existing_georef_rotation_does_not_swing_about_the_origin(client):
    job_id, status = prepared(client, HOUSE_GEOREF)
    g = status["meta"]["georef"]
    # re-edit: the file's placement pinned at the IFC origin, then re-pinned on the footprint centre
    restored = solve(client, job_id, {"epsg": 32643, "rotation_deg": g["rotation_deg"], "height": g["orthogonal_height"],
                                      "scale": {"mode": "fixed", "value": g["scale"], "source": "file"},
                                      "anchor": {"local": [0, 0], "map": [g["eastings"], g["northings"]]}})
    assert restored["solution"]["eastings"] == pytest.approx(g["eastings"], abs=1e-9)
    assert restored["solution"]["scale_source"] == "file"
    pivot_local, pivot_map = restored["client"]["pivot_local"], restored["client"]["pivot_map"]
    turned = solve(client, job_id, {"epsg": 32643, "rotation_deg": g["rotation_deg"] + 45, "height": 0,
                                    "scale": {"mode": "fixed", "value": g["scale"], "source": "file"},
                                    "anchor": {"local": pivot_local, "map": pivot_map}})
    centre = solver.apply(turned["solution"], [(pivot_local[0], pivot_local[1], 0.0)])[0]
    assert centre[:2] == pytest.approx(pivot_map, abs=0.001)


# ---- R4: scale by distance and scale sources -------------------------------------------
def test_scale_by_distance_keeps_the_first_point(client):
    job_id, _ = prepared(client, HOUSE)
    p1, p2 = [-9234.8, -2746.4, 0.0], [758.3, -2746.4, 0.0]           # 9993.1 mm apart in the model
    base = solve(client, job_id, {"epsg": 27700, "use_site": True})
    p1_map = solver.apply(base["solution"], [tuple(p1)])[0]
    scaled = solve(client, job_id, {
        "epsg": 27700, "use_site": True, "rotation_deg": base["solution"]["rotation_deg"],
        "anchor": {"local": p1[:2], "map": list(p1_map[:2])},
        "scale": {"mode": "fixed", "source": "distance", "from_distance": {"p1": p1, "p2": p2, "distance_m": 9.9931}}})
    sol = scaled["solution"]
    assert sol["scale_source"] == "distance" and sol["scale_label"] == "From distance"
    assert sol["scale"] == pytest.approx(0.001, rel=0.005)
    assert solver.apply(sol, [tuple(p1)])[0][:2] == pytest.approx(p1_map[:2], abs=1e-6)
    assert sol["rotation_deg"] == pytest.approx(base["solution"]["rotation_deg"])


def test_scale_sources_unit_override_and_warning(client, tmp_path):
    duplex_like = make_ifc2x3(tmp_path / "feet.ifc")
    job_id, _ = prepared(client, duplex_like)
    pts = {"epsg": 2056, "use_site": True}
    sol = solve(client, job_id, pts)["solution"]
    assert sol["scale_label"] == "File units (ft → 0.3048)"
    over = solve(client, job_id, {**pts, "unit_override": "m"})["solution"]
    assert over["scale"] == pytest.approx(1.0) and "unit override" in over["scale_label"]
    manual = solve(client, job_id, {**pts, "scale": {"mode": "fixed", "value": 0.31, "source": "manual"}})["solution"]
    assert manual["scale_label"] == "Manual"
    assert any(w["code"] == "scale_deviation" for w in manual["warnings"])


# ---- R10.3 MapUnit, R10.4 IfcMapConversionScaled, R7d GDH pset -------------------------------
SOL = {"eastings": 2683000.0, "northings": 1248000.0, "orthogonal_height": 408.0,
       "x_axis_abscissa": 1.0, "x_axis_ordinate": 0.0, "scale": 0.001}


def test_map_unit_is_written(tmp_path):
    meta = model_info.extract(ifcopenshell.open(str(HOUSE)))
    out = tmp_path / "m.ifc"
    writer.write(HOUSE, out, meta, SOL, crs.details(2056))
    unit = ifcopenshell.open(str(out)).by_type("IfcProjectedCRS")[0].MapUnit
    assert unit.is_a("IfcSIUnit") and unit.Name == "METRE" and not unit.Prefix
    out_ft = tmp_path / "ft.ifc"
    writer.write(HOUSE, out_ft, meta, SOL, crs.details(2263))  # NAD83 / New York Long Island (ftUS)
    unit = ifcopenshell.open(str(out_ft)).by_type("IfcProjectedCRS")[0].MapUnit
    assert unit.is_a("IfcConversionBasedUnit")
    assert unit.ConversionFactor.ValueComponent.wrappedValue == pytest.approx(0.3048006096)


def make_scaled(path):
    f = ifcopenshell.file(schema="IFC4X3_ADD2")
    axis = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None)
    ctx = f.createIfcGeometricRepresentationContext(None, "Model", 3, 1e-5, axis, None)
    metre = f.createIfcSIUnit(None, "LENGTHUNIT", None, "METRE")
    f.createIfcProject(ifcopenshell.guid.new(), None, "P", None, None, None, None, [ctx],
                       f.createIfcUnitAssignment([metre]))
    target = f.createIfcProjectedCRS("EPSG:2056", None, None, None, None, None, None)
    f.createIfcMapConversionScaled(ctx, target, 2600000.0, 1200000.0, 400.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.9996)
    f.write(str(path))
    return path


def test_map_conversion_scaled_keeps_factors(tmp_path):
    src = make_scaled(tmp_path / "scaled.ifc")
    meta = model_info.extract(ifcopenshell.open(str(src)))
    assert meta["georef"]["factors"] == pytest.approx([1.0, 1.0, 0.9996])
    out = tmp_path / "out.ifc"
    writer.write(src, out, meta, {**SOL, "scale": 1.0}, crs.details(2056))
    f = ifcopenshell.open(str(out))
    ops = f.by_type("IfcMapConversion")
    assert len(ops) == 1 and ops[0].is_a("IfcMapConversionScaled")
    assert (ops[0].FactorZ, ops[0].Eastings) == pytest.approx((0.9996, 2683000.0))


def test_gdh_pset_round_trip(tmp_path):
    meta = model_info.extract(ifcopenshell.open(str(HOUSE)))
    gdh = {"tool": "ifc-georef", "epsg": 2056, "points": [{"label": "P1", "local": [1, 2, 3], "target": [4, 5, 6]}],
           "scale": {"value": 0.001, "source": "distance"}, "note": "Zürich 'quoted' \\ back"}
    first = tmp_path / "a.ifc"
    writer.write(HOUSE, first, meta, SOL, crs.details(2056), gdh=gdh)
    meta2 = model_info.extract(ifcopenshell.open(str(first)))
    assert meta2["gdh"] == gdh
    second = tmp_path / "b.ifc"
    writer.write(first, second, meta2, SOL, crs.details(2056), gdh={**gdh, "epsg": 2056, "note": "v2"})
    f = ifcopenshell.open(str(second))
    assert len([p for p in f.by_type("IfcPropertySet") if p.Name == "GDH_Georeferencing"]) == 1
    assert model_info.extract(f)["gdh"]["note"] == "v2"


# ---- R10.6 EPSG from names / parameters ---------------------------------------------------
def test_resolve_epsg_without_code():
    assert crs.resolve_epsg("CH1903+ / LV95") == 2056
    assert crs.resolve_epsg("Amersfoort / RD New") == 28992
    assert crs.resolve_epsg(None, None, "ETRS89", "UTM", "32N") == 25832
    assert crs.resolve_epsg("totally unknown grid") is None


# ---- R10.5 .ifczip limits ---------------------------------------------------------------
def test_ifczip_limits(tmp_path):
    from ifcgeoref import create_app

    app = create_app({"DATA_DIR": tmp_path / "d", "MAX_CONTENT_LENGTH": 1024 * 1024, "ELEVATION_LOOKUP": "off"})
    client = app.test_client()
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.ifc", "ISO-10303-21;\n" + "#1=IFCWALL($);\n" * 400000)  # ~6 MB unpacked
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(1001):
            zf.writestr(f"f{i}.txt", "x")
    for data, message in ((bomb, "unpacks to more than"), (many, "more than 1000 entries")):
        data.seek(0)
        res = client.post("/api/jobs", data={"file": (data, "model.ifczip")}, content_type="multipart/form-data")
        assert res.status_code == 400 and message in res.get_json()["error"]["message"]


# ---- R7 project round trip (save to project, open from project) -------------------------------
def test_project_round_trip_restores_points(client, monkeypatch, tmp_path):
    import requests

    store = {"house.ifc": HOUSE.read_bytes()}

    class Resp:
        def __init__(self, status, body=b"", data=None):
            self.status_code, self.ok, self._body, self._data = status, status < 400, body, data
            self.text = body.decode("latin1") if body else ""
            self.headers = {"Content-Length": str(len(body))}

        def json(self):
            return self._data

        def iter_content(self, size):
            for i in range(0, len(self._body), size):
                yield self._body[i:i + size]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_get(url, headers=None, timeout=None, stream=False):
        if url.endswith("/api/ifc/P7/"):
            if (headers or {}).get("Authorization") != "Token ok":
                return Resp(401)
            return Resp(200, data={"ifc_files": [
                {"filename": n, "s3_key": f"p7/{n}", "size": len(b), "url": f"https://s3.invalid/{n}"}
                for n, b in store.items()]})
        name = url.rsplit("/", 1)[-1]
        return Resp(200, store[name]) if name in store else Resp(404)

    def fake_post(url, data=None, headers=None, timeout=None):
        body = b"".join(iter(lambda: data.read(1 << 16), b""))
        start = body.index(b"ISO-10303-21")
        name = body.split(b'filename="')[1].split(b'"')[0].decode()
        store[name] = body[start:body.rindex(b"END-ISO-10303-21;") + len(b"END-ISO-10303-21;")] + b"\n"
        return Resp(201, data={"ok": True})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    auth = {"Authorization": "Token ok"}

    assert client.get("/api/project/P7/ifc-files").status_code == 401
    files = client.get("/api/project/P7/ifc-files", headers=auth).get_json()["files"]
    assert files[0]["filename"] == "house.ifc" and "url" not in files[0]

    def open_from_project(s3_key):
        res = client.post("/api/jobs/from-project", json={"project_id": "P7", "s3_key": s3_key}, headers=auth)
        assert res.status_code == 201, res.get_json()
        job = res.get_json()["job_id"]
        _, fetch = wait_task(client, job, "fetch")
        assert fetch["state"] == "done", fetch.get("error")
        status, task = wait_task(client, job, "prepare")
        assert task["state"] == "done", task.get("error")
        return job, status

    job, _ = open_from_project("p7/house.ifc")
    points = [{"label": "A", "local": [0, 0, 0], "target": [530000.0, 180000.0, 10.0]},
              {"label": "B", "local": [10000, 0, 0], "target": [530010.0, 180000.0, 10.0]}]
    payload = {"epsg": 27700, "use_site": False, "points": points,
               "scale": {"mode": "fixed", "value": 0.001, "source": "distance"}}
    client.post(f"/api/jobs/{job}/georeference", json=payload)
    _, task = wait_task(client, job, "georeference")
    assert task["state"] == "done", task.get("error")
    client.post(f"/api/jobs/{job}/upload", json={"project_id": "P7", "filename": "house_georef.ifc"}, headers=auth)
    _, task = wait_task(client, job, "upload")
    assert task["state"] == "done" and task["result"]["filename"] == "house_georef.ifc"

    _, status = open_from_project("p7/house_georef.ifc")
    gdh = status["meta"]["gdh"]
    assert gdh["epsg"] == 27700
    assert [p["label"] for p in gdh["points"]] == ["A", "B"]
    assert gdh["points"][1]["target"] == [530010.0, 180000.0, 10.0]
    assert gdh["scale"]["source"] == "distance" and gdh["scale"]["value"] == pytest.approx(0.001)
    assert status["meta"]["georef"]["epsg"] == 27700


# ---- R11 analysis API ---------------------------------------------------------------------
def test_analysis_reproduces_written_values_and_mesh_bbox(client, tmp_path):
    meta = model_info.extract(ifcopenshell.open(str(HOUSE)))
    sol = {"eastings": 530157.9, "northings": 179576.9, "orthogonal_height": 12.5,
           "x_axis_abscissa": math.cos(math.radians(20)), "x_axis_ordinate": math.sin(math.radians(20)), "scale": 0.001}
    out = tmp_path / "georef.ifc"
    writer.write(HOUSE, out, meta, sol, crs.details(27700))
    with open(out, "rb") as fh:
        data = client.post("/api/analyze-georeferencing", data={"file": (fh, "georef.ifc")},
                           content_type="multipart/form-data").get_json()
    t = data["transform"]
    assert t["epsg"] == 27700 and t["file_unit_m"] == pytest.approx(0.001)
    assert (t["eastings"], t["northings"], t["orthogonal_height"], t["scale"]) == (530157.9, 179576.9, 12.5, 0.001)
    assert t["rotation_deg_ccw_from_east"] == pytest.approx(20) and t["rotation_deg_clockwise"] == pytest.approx(-20)
    assert data["world_coordinates"]["three_js"]["x"] == pytest.approx(530157.9 * 0.001)

    bounds = geometry.extents(ifcopenshell.open(str(out)))
    lo, hi = [v / 0.001 for v in bounds["min"]], [v / 0.001 for v in bounds["max"]]
    corners = solver.apply(sol, [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2])])
    box = data["bounding_box_world"]
    assert box["source"] == "geometry" and box["crs"] == "EPSG:27700" and len(box["corners"]) == 4
    assert box["min"]["easting"] == pytest.approx(min(c[0] for c in corners), abs=0.01)
    assert box["max"]["northing"] == pytest.approx(max(c[1] for c in corners), abs=0.01)
    assert box["min"]["elevation"] == pytest.approx(bounds["min"][2] + 12.5, abs=0.01)
    assert box["max"]["elevation"] == pytest.approx(bounds["max"][2] + 12.5, abs=0.01)


def test_analysis_unknown_crs_is_not_defaulted(client, tmp_path):
    f = ifcopenshell.open(str(HOUSE))
    meta = model_info.extract(f)
    out = tmp_path / "unknown.ifc"
    writer.write(HOUSE, out, meta, SOL, {**crs.details(2056), "id": "My local grid", "name": "nothing known"})
    with open(out, "rb") as fh:
        data = client.post("/api/analyze-georeferencing", data={"file": (fh, "u.ifc")},
                           content_type="multipart/form-data").get_json()
    assert data["transform"]["epsg"] is None and data["bounding_box_world"]["crs"] is None
    assert any("could not be matched" in i["message"] for i in data["issues"])


# ---- R6 feature edges -----------------------------------------------------------------------
def test_feature_edges_skip_diagonals():
    import numpy as np

    # a unit cube: 12 triangles, 18 triangle edges, 12 real edges (6 face diagonals are not)
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    a, b = geometry.feature_edges(v, f)
    assert len(a) == 12
    assert np.allclose(np.linalg.norm(b - a, axis=1), 1.0)  # no diagonal (length √2)
