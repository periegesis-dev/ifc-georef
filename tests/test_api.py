import gzip
import io

import ifcopenshell
import pytest

from ifcgeoref import model_info
from conftest import HOUSE, HOUSE_GEOREF, make_ifc2x3, upload, wait_task


def test_full_workflow(client, tmp_path):
    res = upload(client, HOUSE)
    assert res.status_code == 201
    job_id = res.get_json()["job_id"]

    status, task = wait_task(client, job_id, "prepare")
    assert task["state"] == "done", task.get("error")
    assert [s["state"] for s in task["steps"]] == ["done"] * 4
    meta, geometry = status["meta"], status["geometry"]
    assert meta["unit"]["symbol"] == "mm" and meta["site"]["has_location"]
    assert geometry["triangles"] > 1000 and len(geometry["footprint_m"]) >= 3

    model = client.get(f"/api/jobs/{job_id}/model.glb", headers={"Accept-Encoding": "gzip"})
    assert model.status_code == 200 and model.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(model.data)[:4] == b"glTF"

    suggestions = client.get("/api/crs/suggest?lat=51.5015&lon=-0.1263").get_json()["suggestions"]
    assert suggestions[0]["code"] == 27700

    payload = {"epsg": 27700, "use_site": True, "points": []}
    solved = client.post(f"/api/jobs/{job_id}/solve", json=payload).get_json()
    sol = solved["solution"]
    assert solved["can_save"] and solved["covers_location"] is True
    assert sol["eastings"] == pytest.approx(530157.90, abs=0.05)
    assert sol["northings"] == pytest.approx(179576.94, abs=0.05)
    assert sol["scale"] == pytest.approx(0.001)
    assert 1.0 < sol["rotation_deg"] < 2.0  # grid convergence of the British grid in London
    assert solved["preview"]["footprint"] and solved["preview"]["origin"]
    # On a web map the model must face true north again: its +Y is true north (TrueNorth 0,1),
    # so +X points due east once the grid convergence is taken out.
    map3d = solved["preview"]["map3d"]
    assert abs(map3d["heading_deg"]) < 0.02
    assert map3d["scale_ratio"] == pytest.approx(1.0)
    assert map3d["anchor"][0] == pytest.approx(51.50015, abs=2e-4)

    started = client.post(f"/api/jobs/{job_id}/georeference", json=payload)
    assert started.status_code == 202
    status, task = wait_task(client, job_id, "georeference")
    assert task["state"] == "done", task.get("error")
    assert status["output"]["filename"] == "Ifc4_SampleHouse_0_GroundFloor_georeferenced.ifc"

    download = client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 200
    out = tmp_path / "downloaded.ifc"
    out.write_bytes(download.data)
    info = model_info.georef_info(ifcopenshell.open(str(out)))
    assert info["epsg"] == 27700 and info["eastings"] == pytest.approx(sol["eastings"])

    again = upload(client, HOUSE, "copy.ifc").get_json()
    assert again["cached"] is True
    assert again["tasks"]["prepare"]["state"] == "done"


def test_outside_area_blocks_saving(client):
    job_id = upload(client, HOUSE).get_json()["job_id"]
    wait_task(client, job_id, "prepare")
    payload = {"epsg": 2056, "use_site": True}
    solved = client.post(f"/api/jobs/{job_id}/solve", json=payload).get_json()
    assert solved["can_save"] is False
    assert solved["solution"]["warnings"][0]["code"] == "outside_area"
    assert client.post(f"/api/jobs/{job_id}/georeference", json=payload).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/solve", json={**payload, "allow_outside": True}).get_json()["can_save"]


def test_ifc2x3_feet_with_control_points(client, tmp_path):
    path = make_ifc2x3(tmp_path / "duplex_like.ifc")
    job_id = upload(client, path).get_json()["job_id"]
    status, task = wait_task(client, job_id, "prepare")
    assert task["state"] == "done", task.get("error")
    assert status["meta"]["unit"]["symbol"] == "ft"
    assert status["geometry"]["empty"] is True
    payload = {"epsg": 2056, "use_site": False, "points": [
        {"label": "A", "local": [0, 0, 0], "target": [2683000, 1248000, 408]},
        {"label": "B", "local": [100, 0, 0], "target": [2683030.48, 1248000, 408]},
    ]}
    solved = client.post(f"/api/jobs/{job_id}/solve", json=payload).get_json()
    assert solved["solution"]["scale"] == pytest.approx(0.3048)
    assert solved["solution"]["rms_horizontal_m"] < 0.01
    client.post(f"/api/jobs/{job_id}/georeference", json=payload)
    _, task = wait_task(client, job_id, "georeference")
    assert task["state"] == "done", task.get("error")


def test_upload_to_project(client, monkeypatch):
    import requests

    calls = []

    class Response:
        def __init__(self, status):
            self.status_code, self.ok, self.text = status, status < 400, "{}"

        def json(self):
            return {"id": 7}

    def fake_post(url, data=None, headers=None, timeout=None):
        body = b""
        while True:
            chunk = data.read(65536)
            if not chunk:
                break
            body += chunk
        calls.append({"url": url, "headers": headers, "body": body})
        return Response(401 if headers.get("Authorization") == "Token bad" else 201)

    monkeypatch.setattr(requests, "post", fake_post)
    job_id = upload(client, HOUSE).get_json()["job_id"]
    wait_task(client, job_id, "prepare")
    assert client.post(f"/api/jobs/{job_id}/upload", json={"project_id": "P1"}).status_code == 409
    client.post(f"/api/jobs/{job_id}/georeference", json={"epsg": 27700, "use_site": True})
    wait_task(client, job_id, "georeference")

    assert client.post(f"/api/jobs/{job_id}/upload", json={"project_id": "../x"}).status_code == 400
    res = client.post(f"/api/jobs/{job_id}/upload", json={"project_id": "OHIPT7"},
                      headers={"Authorization": "Token good"})
    assert res.status_code == 202
    _, task = wait_task(client, job_id, "upload")
    assert task["state"] == "done"
    sent = calls[-1]
    assert sent["url"] == "http://backend.invalid/api/ifc/OHIPT7/upload/"
    assert sent["headers"]["Authorization"] == "Token good"
    assert b"IFCMAPCONVERSION" in sent["body"]  # the georeferenced copy is sent

    client.post(f"/api/jobs/{job_id}/upload", json={"project_id": "OHIPT7"},
                headers={"Authorization": "Token bad"})
    _, task = wait_task(client, job_id, "upload")
    assert task["state"] == "error" and "login" in task["error"].lower()


def test_legacy_analyze_endpoint_keeps_its_shape(client):
    with open(HOUSE_GEOREF, "rb") as fh:
        res = client.post("/api/analyze-georeferencing", data={"file": (fh, "model.ifc")},
                          content_type="multipart/form-data")
    data = res.get_json()
    assert res.status_code == 200
    assert data["is_georeferenced"] is True
    assert data["georeferencing_method"] == "IFC4+ MapConversion"
    mc = data["georeferencing_info"]["map_conversion"]
    assert {"eastings", "northings", "orthogonal_height", "scale"} <= set(mc)
    box = data["bounding_box_world"]
    assert {"easting", "northing", "elevation"} <= set(box["min"])
    # elevation is in map units: scale * z + orthogonal height (not raw millimetres)
    lo_z = data["coordinates_info"]["bounds"]["min_z"]
    assert box["min"]["elevation"] == pytest.approx(lo_z * mc["scale"] + mc["orthogonal_height"])
    assert {"latitude", "longitude"} <= set(data["world_coordinates"]["wgs84"])
    assert {"x", "y", "z"} <= set(data["world_coordinates"]["three_js"])


def test_inputs_and_security(client):
    not_ifc = client.post("/api/jobs", data={"file": (io.BytesIO(b"hello"), "notes.ifc")},
                          content_type="multipart/form-data")
    assert not_ifc.status_code == 400 and "not an IFC file" in not_ifc.get_json()["error"]["message"]
    assert client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "model.txt")},
                       content_type="multipart/form-data").status_code == 415
    assert client.get("/api/jobs/../../etc/passwd").status_code == 404
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404
    assert client.get("/uploads/anything.ifc").status_code == 404
    assert client.get("/templates/index.html").status_code == 404
    assert client.get("/survey/old.ifc").status_code == 302
    assert client.get("/api/crs/4326").get_json()["projected"] is False
    page = client.get("/?project_id=X&token=secret")
    assert page.status_code == 200 and b"secret" not in page.data
    # origin only for other sites (tiles need a Referer), never the path or the query
    assert page.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert client.get("/healthz").get_json() == {"status": "ok"}
