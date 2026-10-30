"""Runtime configuration, read from environment variables (see .env.example)."""
import os
import secrets
from pathlib import Path

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:3001,http://127.0.0.1:3001,"
    "https://dev.geodatahub.ch,https://www.geodatahub.ch"
)


def _int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def load_config():
    root = Path(__file__).resolve().parent.parent
    origins = os.getenv("CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
    return {
        "DATA_DIR": Path(os.getenv("DATA_DIR") or root / "data"),
        "API_BASE_URL": (os.getenv("API_BASE_URL") or "http://localhost:8000").rstrip("/"),
        # Sessions are not used for workflow state, so a random per-process key is fine
        # when none is configured.
        "SECRET_KEY": os.getenv("SECRET_KEY") or secrets.token_hex(32),
        "MAX_CONTENT_LENGTH": _int("MAX_UPLOAD_MB", 500) * 1024 * 1024,
        "CORS_ORIGINS": [o.strip() for o in origins.split(",") if o.strip()],
        "JOB_TTL_HOURS": _float("JOB_TTL_HOURS", 24),
        "CACHE_TTL_HOURS": _float("CACHE_TTL_HOURS", 24 * 7),
        "MAX_PARALLEL_JOBS": max(1, _int("MAX_PARALLEL_JOBS", 2)),
        "GEOMETRY_THREADS": _int("GEOMETRY_THREADS", 0) or (os.cpu_count() or 2),
        "DEFAULT_EPSG": _int("DEFAULT_EPSG", 2056),
        # all | swisstopo | off  — where suggested ground heights may be looked up
        "ELEVATION_LOOKUP": (os.getenv("ELEVATION_LOOKUP") or "all").lower(),
        "UPLOAD_TIMEOUT": _int("UPLOAD_TIMEOUT", 600),
    }
