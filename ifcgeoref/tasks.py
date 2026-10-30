"""Long-running work. Functions here run in separate worker processes (or I/O threads)
and report progress through TaskReporter files."""
import logging
import math
import shutil
import time
import traceback
import uuid
from pathlib import Path

from .progress import TaskReporter, read_json, write_json

log = logging.getLogger(__name__)

# Parse speed used to estimate progress while ifcopenshell reads a file (bytes/second).
# Measured ~10 MB/s on an idle 4-core machine; estimate conservatively.
PARSE_RATE = 5e6

PREPARE_STEPS = [
    ("read", "Reading the IFC file", 30),
    ("info", "Reading units, site and georeferencing", 8),
    ("geometry", "Building the 3D preview", 52),
    ("package", "Packing the 3D model and footprint", 10),
]
GEOREF_STEPS = [
    ("write", "Writing the georeferenced IFC", 35),
    ("verify", "Checking the result", 65),
]
UPLOAD_STEPS = [
    ("send", "Uploading to the project", 95),
    ("confirm", "Waiting for the server to confirm", 5),
]


def _friendly(exc):
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, MemoryError):
        return "The model is too large for the available memory."
    if "Unable to parse" in text or "not a valid" in text.lower():
        return f"The file could not be read as IFC: {text}"
    return text


def _log_error(job_dir, task):
    try:
        with open(Path(job_dir) / f"{task}.error.log", "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
    except OSError:
        pass


def prepare(job_dir, cache_dir=None, threads=2):
    """Read the uploaded IFC once: metadata, existing georeferencing and the 3D preview."""
    import ifcopenshell
    from . import geometry, model_info

    job = Path(job_dir)
    rep = TaskReporter(job / "tasks" / "prepare.json", "prepare", PREPARE_STEPS)
    try:
        src = job / "original.ifc"
        size = src.stat().st_size
        rep.start("read", f"{size / 1e6:.1f} MB")
        with rep.estimate("read", 0.5 + size / PARSE_RATE):
            ifc_file = ifcopenshell.open(str(src))
        rep.done("read", f"{ifc_file.schema}, {size / 1e6:.1f} MB")

        rep.start("info")
        meta = model_info.extract(ifc_file)
        job_info = read_json(job / "job.json", {})
        meta["file"] = {"name": job_info.get("filename"), "size": size}
        write_json(job / "meta.json", meta)
        unit = meta["unit"]
        rep.done("info", f"{meta['element_count']:,} elements, units: {unit['symbol']}")

        if not meta["supported"]:
            rep.done("geometry", "Skipped (file not supported)", state="skipped")
            rep.done("package", state="skipped")
            rep.finish({"supported": False})
            return

        rep.start("geometry")
        glb_path = job / "model.glb.gz"

        def on_package(fraction, detail=None):
            if rep.step_state("geometry") == "running":
                rep.done("geometry")
            if rep.step_state("package") == "pending":
                rep.start("package")
            rep.update("package", fraction, detail)

        geo = geometry.build(ifc_file, glb_path, threads=threads,
                             progress=lambda f, d=None: rep.update("geometry", f, d),
                             package_progress=on_package)
        if rep.step_state("geometry") == "running":
            rep.done("geometry")
        rep.set_detail("geometry", "No 3D geometry found" if geo.get("empty")
                       else f"{geo['shapes']:,} elements")
        if rep.step_state("package") == "pending":
            rep.start("package")
        write_json(job / "geometry.json", geo)
        rep.done("package", "No geometry" if geo.get("empty") else
                 f"{geo['triangles']:,} triangles, {glb_path.stat().st_size / 1e6:.1f} MB")

        if cache_dir:
            try:
                cache = Path(cache_dir)
                cache.mkdir(parents=True, exist_ok=True)
                for name in ("meta.json", "geometry.json", "model.glb.gz"):
                    if (job / name).exists():
                        shutil.copyfile(job / name, cache / f".{name}.part")
                        (cache / f".{name}.part").replace(cache / name)
            except OSError:
                log.warning("could not write cache %s", cache_dir, exc_info=True)
        rep.finish({"supported": True})
    except Exception as exc:  # report and keep the worker process healthy
        _log_error(job, "prepare")
        rep.fail(_friendly(exc))


def _close(a, b, rel=1e-9):
    return a is not None and b is not None and math.isclose(float(a), float(b), rel_tol=rel, abs_tol=1e-6)


def georeference(job_dir, solution, crs):
    """Write the georeferenced copy and read it back to verify it."""
    import ifcopenshell
    from . import model_info, writer

    job = Path(job_dir)
    rep = TaskReporter(job / "tasks" / "georeference.json", "georeference", GEOREF_STEPS)
    out = job / "output.ifc"
    try:
        meta = read_json(job / "meta.json")
        src = job / "original.ifc"
        for stale in ("output.ifc", "output.json"):
            (job / stale).unlink(missing_ok=True)
        rep.start("write")
        summary = writer.write(src, out, meta, solution, crs, progress=lambda f: rep.update("write", f))
        rep.done("write", f"{summary['added']} records added, {summary['replaced']} updated")

        rep.start("verify")
        size = out.stat().st_size
        with rep.estimate("verify", 0.5 + size / PARSE_RATE):
            check = ifcopenshell.open(str(out))
        info = model_info.georef_info(check)
        expected = {k: solution[k] for k in ("eastings", "northings", "orthogonal_height",
                                             "x_axis_abscissa", "x_axis_ordinate", "scale")}
        mismatched = [k for k, v in expected.items() if not _close(info.get(k), v)]
        if not info.get("present") or mismatched or info.get("epsg") != crs["code"]:
            raise ValueError("The written file did not read back correctly "
                             f"({', '.join(mismatched) or 'georeferencing missing'}).")
        rep.done("verify", f"{crs['id']} read back correctly")

        name = Path((read_json(job / "job.json", {}) or {}).get("filename") or "model.ifc").stem
        result = {
            "filename": f"{name}_georeferenced.ifc",
            "bytes": size,
            "solution": solution,
            "crs": {k: crs.get(k) for k in ("code", "id", "name", "unit")},
            "created_at": time.time(),
        }
        write_json(job / "output.json", result)
        rep.finish(result)
    except Exception as exc:
        _log_error(job, "georeference")
        out.unlink(missing_ok=True)
        rep.fail(_friendly(exc))


class _MultipartFile:
    """Streams one file as multipart/form-data with a known length and progress callback."""

    def __init__(self, path, field, filename, progress=None):
        boundary = uuid.uuid4().hex
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ") or "model.ifc"
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self._parts = [
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode(),
            None,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        self._fh = open(path, "rb")
        self._size = Path(path).stat().st_size
        self._total = len(self._parts[0]) + self._size + len(self._parts[2])
        self._index, self._offset, self._sent = 0, 0, 0
        self._progress = progress

    def __len__(self):
        return self._total

    def read(self, n=-1):
        n = 1 << 20 if n is None or n < 0 else n
        out = b""
        while len(out) < n and self._index < 3:
            part = self._parts[self._index]
            if part is None:
                chunk = self._fh.read(n - len(out))
                if not chunk:
                    self._index += 1
                    continue
                out += chunk
            else:
                chunk = part[self._offset:self._offset + n - len(out)]
                self._offset += len(chunk)
                out += chunk
                if self._offset >= len(part):
                    self._index, self._offset = self._index + 1, 0
        self._sent += len(out)
        if self._progress:
            self._progress(self._sent / self._total)
        return out

    def close(self):
        self._fh.close()


UPLOAD_ERRORS = {
    401: "Your GeoDataHub login has expired or is invalid. Log in again and reopen this tool from the project.",
    403: "You don't have permission to upload files to this project.",
    404: "The project was not found on the server.",
    413: "The file is too large for the project server.",
}


def upload_to_project(job_dir, api_base_url, project_id, authorization, timeout=600):
    """Send the georeferenced file to the GeoDataHub backend (runs in an I/O thread)."""
    import requests

    job = Path(job_dir)
    rep = TaskReporter(job / "tasks" / "upload.json", "upload", UPLOAD_STEPS)
    body = None
    try:
        output = read_json(job / "output.json")
        path = job / "output.ifc"
        if not output or not path.exists():
            raise ValueError("Write the georeferenced file before uploading it.")
        body = _MultipartFile(path, "files", output["filename"], lambda f: rep.update("send", f))
        headers = {"Content-Type": body.content_type}
        if authorization:
            headers["Authorization"] = authorization
        rep.start("send", f"{output['bytes'] / 1e6:.1f} MB")
        response = requests.post(f"{api_base_url}/api/ifc/{project_id}/upload/", data=body,
                                 headers=headers, timeout=(15, timeout))
        rep.done("send")
        rep.start("confirm")
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:2000]}
        if not response.ok:
            message = UPLOAD_ERRORS.get(response.status_code) or \
                f"The project server answered {response.status_code}: {str(payload)[:300]}"
            rep.state["result"] = {"status": response.status_code, "response": payload}
            rep.fail(message)
            return
        rep.done("confirm", f"Server answered {response.status_code}")
        rep.finish({"status": response.status_code, "response": payload, "project_id": project_id})
    except requests.exceptions.ConnectionError:
        rep.fail(f"Could not connect to the project server ({api_base_url}).")
    except requests.exceptions.Timeout:
        rep.fail("The project server did not answer in time. Try again later.")
    except Exception as exc:
        _log_error(job, "upload")
        rep.fail(_friendly(exc))
    finally:
        if body is not None:
            body.close()


def legacy_analyze(path, filename):
    import ifcopenshell
    from .legacy import analyze

    return analyze(ifcopenshell.open(str(path)), filename)


def legacy_devs(path, filename):
    import ifcopenshell
    from .legacy import devs_report

    return devs_report(ifcopenshell.open(str(path)), filename)
