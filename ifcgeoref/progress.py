"""Task progress, stored as small JSON files so any process can report or read it."""
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def overall(task):
    steps = task.get("steps") or []
    total = sum(s["weight"] for s in steps) or 1
    done = sum(s["weight"] * (1.0 if s["state"] in ("done", "skipped", "cached") else s["progress"])
               for s in steps)
    return min(1.0, done / total)


def queued_task(name, steps, message="Waiting for a free worker"):
    now = time.time()
    return {
        "name": name, "state": "queued", "message": message, "error": None, "result": None,
        "created_at": now, "started_at": None, "updated_at": now, "ended_at": None,
        "steps": [{"key": k, "label": label, "weight": w, "state": "pending", "progress": 0.0,
                   "detail": None, "started_at": None, "ended_at": None} for k, label, w in steps],
    }


class TaskReporter:
    """Writes a task's state to <job>/tasks/<name>.json (throttled for frequent updates)."""

    def __init__(self, path, name, steps):
        self.path = Path(path)
        previous = read_json(self.path) or {}
        self.state = queued_task(name, steps)
        self.state["created_at"] = previous.get("created_at", self.state["created_at"])
        self.state.update(state="running", message=None, started_at=time.time())
        self._lock = threading.Lock()
        self._last = 0.0
        self._write(force=True)

    def _step(self, key):
        return next(s for s in self.state["steps"] if s["key"] == key)

    def _write(self, force=False):
        now = time.time()
        if not force and now - self._last < 0.25:
            return
        self._last = now
        self.state["updated_at"] = now
        self.state["progress"] = overall(self.state)
        write_json(self.path, self.state)

    def step_state(self, key):
        return self._step(key)["state"]

    def set_detail(self, key, detail):
        with self._lock:
            self._step(key)["detail"] = detail
            self._write(force=True)

    def start(self, key, detail=None):
        with self._lock:
            step = self._step(key)
            step.update(state="running", started_at=time.time(), detail=detail)
            self._write(force=True)

    def update(self, key, fraction, detail=None):
        with self._lock:
            step = self._step(key)
            if step["state"] != "running":
                return
            step["progress"] = min(max(float(fraction), 0.0), 1.0)
            if detail is not None:
                step["detail"] = detail
            self._write()

    def done(self, key, detail=None, state="done"):
        with self._lock:
            step = self._step(key)
            step.update(state=state, progress=1.0, ended_at=time.time())
            if step["started_at"] is None:
                step["started_at"] = step["ended_at"]
            if detail is not None:
                step["detail"] = detail
            self._write(force=True)

    def finish(self, result=None):
        with self._lock:
            self.state.update(state="done", result=result, ended_at=time.time())
            self._write(force=True)

    def fail(self, message):
        with self._lock:
            for step in self.state["steps"]:
                if step["state"] == "running":
                    step.update(state="error", ended_at=time.time())
            self.state.update(state="error", error=str(message), ended_at=time.time())
            self._write(force=True)

    @contextmanager
    def estimate(self, key, expected_seconds, detail=None):
        """Advance a step on a time estimate while a blocking call runs (no callbacks available)."""
        stop = threading.Event()
        started = time.monotonic()
        expected = max(float(expected_seconds), 0.5)

        def run():
            while not stop.wait(0.3):
                t = time.monotonic() - started
                frac = t / expected * 0.9 if t < expected else 0.9 + 0.09 * (1 - math.exp(-(t - expected) / expected))
                self.update(key, frac, detail)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)
