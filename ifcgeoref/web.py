"""HTTP routes: the wizard page, its JSON API and the legacy endpoints."""
import gzip
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from flask import (Blueprint, Response, current_app, jsonify, redirect, render_template, request,
                   send_file)
from werkzeug.utils import secure_filename

from . import crs as crs_mod
from . import elevation, placement, tasks
from .jobs import JobError

log = logging.getLogger(__name__)
bp = Blueprint("web", __name__)
PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _jobs():
    return current_app.extensions["jobs"]


def api_error(message, status=400, code=None):
    return jsonify({"error": {"message": message, "code": code}}), status


def _float_arg(name):
    value = request.args.get(name)
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _upload_name(storage):
    name = secure_filename(storage.filename or "") or "model.ifc"
    if not name.lower().endswith((".ifc", ".ifczip")):
        raise JobError("Only .ifc and .ifczip files are supported.", 415)
    return name


# ---- pages -----------------------------------------------------------------
@bp.get("/")
def index():
    return render_template("index.html", app_config={
        "defaultEpsg": current_app.config["DEFAULT_EPSG"],
        "maxUploadMb": current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        "elevationLookup": current_app.config["ELEVATION_LOOKUP"] != "off",
        "allowedOrigins": current_app.config["CORS_ORIGINS"],
    })


@bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# Pages of the previous multi-page workflow now live in the single-page wizard.
@bp.route("/upload", methods=["GET", "POST"])
@bp.route("/interactive")
@bp.route("/chek", methods=["GET", "POST"])
@bp.route("/convert/<path:_>", methods=["GET", "POST"])
@bp.route("/survey/<path:_>", methods=["GET", "POST"])
@bp.route("/calc/<path:_>", methods=["GET", "POST"])
@bp.route("/show/<path:_>", methods=["GET", "POST"])
@bp.route("/direct_georeference/<path:_>")
def legacy_page(_=None):
    return redirect("/", code=302)


# ---- jobs ------------------------------------------------------------------
@bp.post("/api/jobs")
def create_job():
    storage = request.files.get("file")
    if storage is None or not storage.filename:
        return api_error("Choose an IFC file to upload.")
    job_id, cached = _jobs().create(storage.stream, _upload_name(storage))
    log.info("job %s created (%s)%s", job_id, storage.filename, " from cache" if cached else "")
    return jsonify({"job_id": job_id, "cached": cached, **_jobs().status(job_id)}), 201


@bp.get("/api/jobs/<job_id>")
def job_status(job_id):
    return jsonify(_jobs().status(job_id))


@bp.get("/api/jobs/<job_id>/model.glb")
def job_model(job_id):
    path = _jobs().dir(job_id) / "model.glb.gz"
    if not path.exists():
        return api_error("The 3D preview is not ready yet.", 404)
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        response = send_file(path, mimetype="model/gltf-binary", max_age=86400, conditional=True,
                             etag=True)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        return response
    return Response(gzip.decompress(path.read_bytes()), mimetype="model/gltf-binary")


@bp.get("/api/jobs/<job_id>/edges.bin")
def job_edges(job_id):
    """Feature edges for snapping: float32 segments (x1, y1, z1, x2, y2, z2) in the glTF frame."""
    path = _jobs().dir(job_id) / "edges.bin.gz"
    if not path.exists():
        return api_error("No snapping edges for this file.", 404)
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        response = send_file(path, mimetype="application/octet-stream", max_age=86400, conditional=True, etag=True)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        return response
    return Response(gzip.decompress(path.read_bytes()), mimetype="application/octet-stream")


def _placement_inputs(job_id):
    jobs = _jobs()
    meta = jobs.read(job_id, "meta.json")
    if not meta:
        raise JobError("The file is still being read. Try again in a moment.", 409)
    return meta, jobs.read(job_id, "geometry.json")


@bp.post("/api/jobs/<job_id>/solve")
def job_solve(job_id):
    meta, geometry = _placement_inputs(job_id)
    return jsonify(placement.compute(meta, geometry, request.get_json(silent=True) or {}))


@bp.post("/api/jobs/<job_id>/georeference")
def job_georeference(job_id):
    jobs = _jobs()
    meta, geometry = _placement_inputs(job_id)
    payload = request.get_json(silent=True) or {}
    result = placement.compute(meta, geometry, payload)
    if not result["can_save"]:
        return api_error(result["solution"]["warnings"][0]["message"], 422, "outside_area")
    jobs.submit_process(job_id, "georeference", tasks.georeference, str(jobs.dir(job_id)),
                        result["solution"], result["crs"], result["gdh"])
    return jsonify({"started": True, **jobs.status(job_id)}), 202


@bp.get("/api/jobs/<job_id>/download")
def job_download(job_id):
    jobs = _jobs()
    output = jobs.read(job_id, "output.json")
    path = jobs.dir(job_id) / "output.ifc"
    if not output or not path.exists():
        return api_error("The georeferenced file has not been written yet.", 404)
    return send_file(path, mimetype="application/x-step", as_attachment=True,
                     download_name=output["filename"])


@bp.post("/api/jobs/<job_id>/upload")
def job_upload(job_id):
    jobs = _jobs()
    payload = request.get_json(silent=True) or {}
    project_id = str(payload.get("project_id") or "").strip()
    if not PROJECT_ID.match(project_id):
        return api_error("A valid project id is needed to upload to a project.")
    if not jobs.read(job_id, "output.json"):
        return api_error("Write the georeferenced file first.", 409)
    authorization = (request.headers.get("Authorization") or "").strip()[:1000] or None
    jobs.submit_thread(job_id, "upload", tasks.upload_to_project, str(jobs.dir(job_id)),
                       current_app.config["API_BASE_URL"], project_id, authorization,
                       current_app.config["UPLOAD_TIMEOUT"], payload.get("filename") or None)
    return jsonify({"started": True, **jobs.status(job_id)}), 202


# ---- GeoDataHub project files -------------------------------------------------
PROJECT_ERRORS = {
    401: "Your GeoDataHub login has expired or is invalid. Log in again and reopen this tool from the project.",
    403: "You don't have access to this project's files.",
    404: "The project was not found on the server.",
}


def _project_files(project_id):
    """The project's IFC files from the GeoDataHub backend (same call as the frontend's BIM tab)."""
    import requests

    if not PROJECT_ID.match(project_id or ""):
        raise JobError("A valid project id is needed.")
    headers = {}
    authorization = (request.headers.get("Authorization") or "").strip()[:1000]
    if authorization:
        headers["Authorization"] = authorization
    try:
        response = requests.get(f"{current_app.config['API_BASE_URL']}/api/ifc/{project_id}/",
                                headers=headers, timeout=(10, 60))
    except requests.exceptions.RequestException:
        raise JobError("Could not reach the project server.", 502) from None
    if not response.ok:
        raise JobError(PROJECT_ERRORS.get(response.status_code,
                                          f"The project server answered {response.status_code}."),
                       response.status_code if response.status_code in PROJECT_ERRORS else 502)
    try:
        data = response.json()
    except ValueError:
        raise JobError("The project server sent an unexpected answer.", 502) from None
    files = data.get("ifc_files") if isinstance(data, dict) else data
    return [f for f in (files or []) if isinstance(f, dict) and f.get("s3_key")]


@bp.get("/api/project/<project_id>/ifc-files")
def project_files(project_id):
    files = _project_files(project_id)
    return jsonify({"files": [{k: f.get(k) for k in ("filename", "s3_key", "size", "last_modified")}
                              for f in files]})


@bp.post("/api/jobs/from-project")
def create_job_from_project():
    payload = request.get_json(silent=True) or {}
    project_id = str(payload.get("project_id") or "")
    s3_key = str(payload.get("s3_key") or "")
    if not s3_key:
        return api_error("Choose a file of the project.")
    jobs = _jobs()
    job_id = jobs.create_from_project(_project_files(project_id), s3_key, project_id)
    log.info("job %s created from project %s", job_id, project_id)
    return jsonify({"job_id": job_id, **jobs.status(job_id)}), 201


# ---- coordinate systems & helpers -------------------------------------------
@bp.get("/api/crs/suggest")
def crs_suggest():
    lat, lon = _float_arg("lat"), _float_arg("lon")
    return jsonify({"suggestions": crs_mod.suggest(lat, lon),
                    "default_epsg": current_app.config["DEFAULT_EPSG"]})


@bp.get("/api/crs/search")
def crs_search():
    return jsonify({"results": crs_mod.search(request.args.get("q", ""),
                                              _float_arg("lat"), _float_arg("lon"))})


@bp.get("/api/crs/<int:code>")
def crs_details(code):
    info = dict(crs_mod.details(code))
    info["covers_location"] = crs_mod.covers(info, _float_arg("lat"), _float_arg("lon"))
    return jsonify(info)


@bp.post("/api/crs/<int:code>/convert")
def crs_convert(code):
    crs_mod.require_projected(code)
    payload = request.get_json(silent=True) or {}
    points = payload.get("points") or []
    if len(points) > 5000:
        return api_error("Too many points.")
    try:
        if payload.get("to") == "wgs84":
            out = crs_mod.to_wgs84(code, [(float(p["e"]), float(p["n"])) for p in points])
            return jsonify({"points": [None if lat is None else {"lat": lat, "lon": lon} for lon, lat in out]})
        out = crs_mod.to_grid(code, [(float(p["lon"]), float(p["lat"])) for p in points])
        return jsonify({"points": [None if e is None else {"e": e, "n": n} for e, n in out]})
    except (KeyError, TypeError, ValueError):
        return api_error("Points must be objects with lat/lon (or e/n) numbers.")


@bp.get("/api/elevation")
def ground_height():
    lat, lon = _float_arg("lat"), _float_arg("lon")
    if lat is None or lon is None:
        return api_error("lat and lon are required.")
    return jsonify(elevation.lookup(lat, lon, current_app.config["ELEVATION_LOOKUP"]) or {"height_m": None})


# ---- legacy public API --------------------------------------------------------
def _legacy_temp_file(storage):
    tmp_dir = Path(current_app.config["DATA_DIR"]) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{uuid.uuid4().hex}.ifc"
    with open(path, "wb") as fh:
        shutil.copyfileobj(storage.stream, fh, 1 << 20)
    return path


@bp.post("/api/analyze-georeferencing")
def analyze_georeferencing():
    storage = request.files.get("file")
    if storage is None or not (storage.filename or "").lower().endswith(".ifc"):
        return jsonify({"error": "Invalid file. Please upload an IFC file.", "is_georeferenced": False}), 400
    name = secure_filename(storage.filename) or "model.ifc"
    path = _legacy_temp_file(storage)
    try:
        jobs = _jobs()
        return jsonify(jobs.run_sync(tasks.legacy_analyze, str(path), name, str(jobs.cache_dir), jobs.threads))
    except Exception as exc:
        log.warning("analyze-georeferencing failed: %s", exc)
        return jsonify({"error": f"Error analyzing IFC file: {exc}", "is_georeferenced": False}), 500
    finally:
        path.unlink(missing_ok=True)


@bp.post("/devs")
def devs():
    storage = request.files.get("file")
    if storage is None or not (storage.filename or "").lower().endswith(".ifc"):
        return Response("No IFC file uploaded\n", status=400, mimetype="text/plain")
    path = _legacy_temp_file(storage)
    try:
        text = _jobs().run_sync(tasks.legacy_devs, str(path), secure_filename(storage.filename))
        return Response(text, mimetype="text/plain")
    finally:
        path.unlink(missing_ok=True)
