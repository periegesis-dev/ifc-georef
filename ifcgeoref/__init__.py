"""IFC georeferencing web application."""
import hashlib
import logging
from pathlib import Path

from flask import Flask, jsonify, render_template, request, url_for
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from .config import load_config
from .crs import CRSError
from .jobs import JobError, Jobs
from .placement import PlacementError

__version__ = "2.0.0"


def _asset_version(static_dir):
    digest = hashlib.sha1()
    for path in sorted(Path(static_dir).rglob("*")):
        if path.is_file():
            digest.update(f"{path.relative_to(static_dir)}:{path.stat().st_mtime_ns}".encode())
    return digest.hexdigest()[:10]


def create_app(overrides=None):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__)
    app.config.update(load_config())
    app.config.update(overrides or {})
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
         supports_credentials=True, expose_headers=["Content-Disposition"])

    app.extensions["jobs"] = Jobs(app.config)

    from .web import api_error, bp
    app.register_blueprint(bp)

    version = _asset_version(app.static_folder)

    @app.context_processor
    def helpers():
        return {"asset": lambda name: url_for("static", filename=name, v=version), "app_version": __version__}

    frame_ancestors = " ".join(["'self'"] + app.config["CORS_ORIGINS"])

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Other sites (map tiles, address search) only ever see the origin, never the
        # path or query string. OSM's tile servers refuse requests without a Referer.
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Content-Security-Policy", f"frame-ancestors {frame_ancestors}")
        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    def wants_json():
        return request.path.startswith("/api/") or request.path == "/devs"

    @app.errorhandler(JobError)
    def job_error(exc):
        return api_error(str(exc), exc.status)

    @app.errorhandler(PlacementError)
    @app.errorhandler(CRSError)
    def input_error(exc):
        return api_error(str(exc), 422)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {
            404: "This page does not exist.",
            405: "This action is not allowed here.",
            413: f"The file is larger than the {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB limit.",
        }
        message = messages.get(exc.code, exc.description)
        if wants_json():
            return api_error(message, exc.code)
        return render_template("error.html", status=exc.code, message=message), exc.code

    @app.errorhandler(Exception)
    def unexpected(exc):
        app.logger.exception("Unhandled error on %s", request.path)
        message = "Something went wrong on the server. Please try again; if it keeps happening, contact support."
        if wants_json():
            return api_error(message, 500)
        return render_template("error.html", status=500, message=message), 500

    return app
