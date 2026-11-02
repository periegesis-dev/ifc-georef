"""Job storage and background execution.

Every upload becomes a job folder  <DATA_DIR>/jobs/<32 hex id>/  holding the original
file, metadata, the 3D preview, task progress files and the result. Heavy IFC work
runs in a process pool (one fresh process per task, so memory is returned and a
crash cannot take the web server down); network uploads run in a thread pool.
"""
import hashlib
import logging
import multiprocessing
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from . import tasks
from .model_info import META_VERSION
from .progress import overall, queued_task, read_json, write_json

log = logging.getLogger(__name__)

JOB_ID = re.compile(r"^[0-9a-f]{32}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
CACHED_FILES = ("meta.json", "geometry.json", "model.glb.gz", "edges.bin.gz")
REQUIRED_CACHED = ("meta.json", "geometry.json")
TASK_STEPS = {"prepare": tasks.PREPARE_STEPS, "georeference": tasks.GEOREF_STEPS,
              "upload": tasks.UPLOAD_STEPS, "fetch": tasks.FETCH_STEPS}
MAX_ZIP_ENTRIES = 1000


class JobError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Jobs:
    def __init__(self, config):
        self.root = Path(config["DATA_DIR"])
        self.jobs_dir = self.root / "jobs"
        self.cache_dir = self.root / "cache"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = config["MAX_PARALLEL_JOBS"]
        self.threads = config["GEOMETRY_THREADS"]
        self.job_ttl = config["JOB_TTL_HOURS"] * 3600
        self.cache_ttl = config["CACHE_TTL_HOURS"] * 3600
        self.max_bytes = config["MAX_CONTENT_LENGTH"]
        self.max_unpacked = config["MAX_CONTENT_LENGTH"] * 4
        self._pool = None
        self._pool_lock = threading.Lock()
        self._io = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job-io")
        self._recover()
        self._janitor = threading.Thread(target=self._cleanup_loop, daemon=True, name="job-cleanup")
        self._janitor.start()

    # ---- paths -------------------------------------------------------------
    def dir(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            raise JobError("Unknown job.", 404)
        path = self.jobs_dir / job_id
        if not (path / "job.json").exists():
            raise JobError("This job does not exist or has expired. Please upload the file again.", 404)
        return path

    # ---- creation ----------------------------------------------------------
    def _new_job(self, filename, source=None):
        job_id = uuid.uuid4().hex
        job = self.jobs_dir / job_id
        (job / "tasks").mkdir(parents=True)
        write_json(job / "job.json", {"id": job_id, "filename": filename, "created_at": time.time(),
                                      "source": source or {"type": "upload"}})
        return job_id, job

    def create(self, stream, filename):
        """Store an uploaded file (streamed) and start processing it."""
        job_id, job = self._new_job(filename)
        try:
            with open(job / "upload.part", "wb") as fh:
                shutil.copyfileobj(stream, fh, 1 << 20)
            cached = self._finish_upload(job)
        except Exception:
            shutil.rmtree(job, ignore_errors=True)
            raise
        return job_id, cached

    def _finish_upload(self, job):
        """upload.part -> original.ifc (unzipped, checked, hashed), then reuse the cache or start reading."""
        raw = job / "upload.part"
        size = raw.stat().st_size
        if size == 0:
            raise JobError("The uploaded file is empty.")
        original = job / "original.ifc"
        if zipfile.is_zipfile(raw):
            self._extract_ifczip(raw, original)
            raw.unlink()
        else:
            raw.replace(original)
        with open(original, "rb") as fh:
            head = fh.read(2048)
        if b"ISO-10303-21" not in head:
            raise JobError("This is not an IFC file. Please upload an .ifc (STEP) or .ifczip file.")
        digest = hashlib.sha256()
        with open(original, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        info = read_json(job / "job.json", {})
        info.update(size=original.stat().st_size, upload_size=size, sha256=sha)
        write_json(job / "job.json", info)
        if self._use_cache(job, sha, info.get("filename")):
            return True
        self.submit_process(job.name, "prepare", tasks.prepare, str(job), str(self.cache_dir / sha), self.threads)
        return False

    def _extract_ifczip(self, archive, target):
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise JobError(f"The .ifczip archive has more than {MAX_ZIP_ENTRIES} entries.")
            if sum(m.file_size for m in entries) > self.max_unpacked:
                raise JobError(f"The .ifczip archive unpacks to more than "
                               f"{self.max_unpacked // (1024 * 1024)} MB.")
            members = [m for m in entries if m.filename.lower().endswith(".ifc") and not m.is_dir()]
            if not members:
                raise JobError("The .ifczip archive does not contain an .ifc file.")
            written = 0
            with zf.open(max(members, key=lambda m: m.file_size)) as src, open(target, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    written += len(chunk)
                    if written > self.max_unpacked:  # do not trust the sizes in the archive header
                        raise JobError("The .ifczip archive unpacks to more than the allowed size.")
                    dst.write(chunk)

    def create_from_project(self, files, s3_key, project_id):
        """Start a job from a file of a GeoDataHub project (entry of the project's file listing)."""
        entry = next((f for f in files if f.get("s3_key") == s3_key), None)
        if entry is None:
            raise JobError("That file is not in the project (any more).", 404)
        url = entry.get("url") or ""
        if not url.lower().startswith(("https://", "http://")):
            raise JobError("The project server gave no download address for this file.", 502)
        filename = entry.get("filename") or s3_key.rsplit("/", 1)[-1] or "model.ifc"
        job_id, job = self._new_job(filename, {"type": "project", "project_id": project_id, "s3_key": s3_key})
        self.submit_thread(job_id, "fetch", tasks.fetch_from_project, str(job), url, self.max_bytes,
                           self._after_fetch)
        return job_id

    def _after_fetch(self, job_id):
        job = self.jobs_dir / job_id
        try:
            self._finish_upload(job)
        except JobError as exc:
            write_json(job / "tasks" / "prepare.json", {**queued_task("prepare", TASK_STEPS["prepare"]),
                                                         "state": "error", "error": str(exc)})

    def _use_cache(self, job, sha, filename):
        cache = self.cache_dir / sha
        meta = read_json(cache / "meta.json")
        if not meta or meta.get("version") != META_VERSION:
            return False
        if not all((cache / name).exists() for name in REQUIRED_CACHED):
            return False
        geometry = read_json(cache / "geometry.json", {})
        if not geometry.get("empty") and not (cache / "model.glb.gz").exists():
            return False
        for name in CACHED_FILES:
            if (cache / name).exists():
                shutil.copyfile(cache / name, job / name)
        meta["file"] = {"name": filename, "size": (job / "original.ifc").stat().st_size}
        write_json(job / "meta.json", meta)
        task = queued_task("prepare", TASK_STEPS["prepare"])
        now = time.time()
        for step in task["steps"]:
            step.update(state="cached", progress=1.0, started_at=now, ended_at=now,
                        detail="Reused from an earlier upload of the same file")
        task.update(state="done", started_at=now, ended_at=now, message="Reused earlier results",
                    result={"supported": meta.get("supported", True), "cached": True})
        write_json(job / "tasks" / "prepare.json", task)
        (cache / "meta.json").touch()
        return True

    # ---- execution ---------------------------------------------------------
    def _process_pool(self):
        with self._pool_lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    max_workers=self.max_workers, mp_context=multiprocessing.get_context("spawn"),
                    max_tasks_per_child=1)
            return self._pool

    def _reset_pool(self, pool):
        with self._pool_lock:
            if self._pool is pool:
                self._pool = None
        pool.shutdown(wait=False, cancel_futures=False)

    def is_running(self, job_id, name):
        state = (read_json(self.dir(job_id) / "tasks" / f"{name}.json") or {}).get("state")
        return state in ("queued", "running")

    def _submit(self, executor, job_id, name, fn, *args):
        path = self.dir(job_id) / "tasks" / f"{name}.json"
        if (read_json(path) or {}).get("state") in ("queued", "running"):
            raise JobError("This step is already running.", 409)
        write_json(path, queued_task(name, TASK_STEPS[name]))
        try:
            future = executor.submit(fn, *args)
        except BrokenProcessPool:
            self._reset_pool(executor)
            executor = self._process_pool()
            future = executor.submit(fn, *args)
        future.add_done_callback(lambda f: self._finished(path, executor, f))
        return future

    def submit_process(self, job_id, name, fn, *args):
        return self._submit(self._process_pool(), job_id, name, fn, *args)

    def submit_thread(self, job_id, name, fn, *args):
        return self._submit(self._io, job_id, name, fn, *args)

    def _finished(self, path, executor, future):
        exc = future.exception()
        state = read_json(path) or {}
        if state.get("state") in ("done", "error"):
            return
        if isinstance(exc, BrokenProcessPool):
            if isinstance(executor, ProcessPoolExecutor):
                self._reset_pool(executor)
            message = ("The processing worker stopped unexpectedly, most likely because it ran out of "
                       "memory. Try again, or give Docker more memory for very large models.")
        elif exc is not None:
            message = str(exc) or exc.__class__.__name__
        else:
            message = "The task ended without reporting a result."
        log.error("task %s failed: %s", path, message)
        state.update(state="error", error=message, ended_at=time.time())
        for step in state.get("steps", []):
            if step.get("state") in ("running", "pending"):
                step["state"] = "error" if step.get("state") == "running" else "pending"
        write_json(path, state)

    def run_sync(self, fn, *args, timeout=600):
        """Run fn in a worker process and wait for its return value."""
        pool = self._process_pool()
        try:
            return pool.submit(fn, *args).result(timeout=timeout)
        except BrokenProcessPool:
            self._reset_pool(pool)
            raise JobError("The processing worker stopped unexpectedly (out of memory?).", 500)

    # ---- status ------------------------------------------------------------
    def status(self, job_id):
        job = self.dir(job_id)
        info = read_json(job / "job.json", {})
        task_states = {}
        for path in sorted((job / "tasks").glob("*.json")):
            task = read_json(path)
            if task:
                task["progress"] = overall(task)
                task_states[path.stem] = task
        return {
            "job": {k: info.get(k) for k in ("id", "filename", "size", "created_at", "source")},
            "tasks": task_states,
            "meta": read_json(job / "meta.json"),
            "geometry": read_json(job / "geometry.json"),
            "output": read_json(job / "output.json"),
        }

    def read(self, job_id, name):
        return read_json(self.dir(job_id) / name)

    # ---- maintenance -------------------------------------------------------
    def _recover(self):
        """Tasks that were running when the service stopped can never finish."""
        for path in self.jobs_dir.glob("*/tasks/*.json"):
            task = read_json(path)
            if task and task.get("state") in ("queued", "running"):
                task.update(state="error", ended_at=time.time(),
                            error="Interrupted because the service restarted. Please start this step again.")
                for step in task.get("steps", []):
                    if step.get("state") == "running":
                        step["state"] = "error"
                write_json(path, task)

    def cleanup(self):
        now = time.time()
        for job in self.jobs_dir.iterdir():
            if not job.is_dir():
                continue
            info = read_json(job / "job.json") or {}
            created = info.get("created_at") or job.stat().st_mtime
            busy = any((read_json(p) or {}).get("state") in ("queued", "running")
                       for p in (job / "tasks").glob("*.json"))
            if now - created > self.job_ttl and not busy:
                shutil.rmtree(job, ignore_errors=True)
        for entry in self.cache_dir.iterdir():
            meta = entry / "meta.json"
            stamp = meta.stat().st_mtime if meta.exists() else entry.stat().st_mtime
            if not SHA.match(entry.name) or now - stamp > self.cache_ttl:
                shutil.rmtree(entry, ignore_errors=True)

    def _cleanup_loop(self):
        while True:
            try:
                self.cleanup()
            except Exception:
                log.warning("cleanup failed", exc_info=True)
            time.sleep(1800)
