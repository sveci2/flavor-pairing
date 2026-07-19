"""Application factory for the read-only flavor-pairing HTTP API (CP9).

``create_app(package_dir=...)`` loads one ``FlavorPackage`` snapshot at
startup (never per request) and stores it under ``app.extensions``; all
request handlers only read that snapshot, so the API is read-only by
construction. Package directory precedence: explicit argument, then the
``FLAVOR_PACKAGE_DIR`` environment variable, then ``data/sample``.

The resolved directory (symlinks followed) is refused when any exact
path component equals the private-data marker — the API must never serve
rights-restricted data. If the package cannot be loaded, the app still
starts in a degraded state: ``/health`` and every ``/api/v1`` route
return 503, response bodies carry no filesystem path or exception text,
and the detailed error goes to the server log only.

JSON output is deterministic: sorted keys, raw UTF-8, and the shared
``flavor_pairing.serialization`` payloads (NULL stays ``null``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from flavor_pairing.query import FlavorPackage, QueryError
from flavor_pairing.validation import PRIVATE_PATH_MARKER
from flavor_pairing_api.routes import EXTENSION_KEY, api_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_DIR = REPO_ROOT / "data" / "sample"
PACKAGE_DIR_ENV_VAR = "FLAVOR_PACKAGE_DIR"


class PrivatePackageDirError(ValueError):
    """The configured package directory resolves into the private data path."""


def resolve_package_dir(package_dir: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the package directory to serve and enforce the private boundary.

    Precedence: explicit ``package_dir`` argument, then the
    ``FLAVOR_PACKAGE_DIR`` environment variable, then ``data/sample``.
    The path is fully resolved (symlinks followed) before checking, and
    is rejected only when an exact resolved component equals the marker —
    a longer directory name merely containing it is not the private path.
    """
    if package_dir is None:
        env_value = os.environ.get(PACKAGE_DIR_ENV_VAR)
        package_dir = Path(env_value) if env_value else DEFAULT_PACKAGE_DIR
    resolved = Path(package_dir).resolve()
    if PRIVATE_PATH_MARKER in resolved.parts:
        raise PrivatePackageDirError(
            "refusing to serve a package from the private data directory"
        )
    return resolved


def create_app(package_dir: Optional[Union[str, Path]] = None) -> Flask:
    directory = resolve_package_dir(package_dir)
    app = Flask(__name__)
    app.json.sort_keys = True
    app.json.ensure_ascii = False

    try:
        package: Optional[FlavorPackage] = FlavorPackage.load(directory)
    except QueryError as exc:
        # Server log only; QueryError text may contain filesystem paths,
        # which must never reach a response body.
        app.logger.error("flavor package failed to load: %s", exc)
        package = None
    app.extensions[EXTENSION_KEY] = package

    app.register_blueprint(api_v1, url_prefix="/api/v1")

    @app.get("/health")
    def health():
        if app.extensions[EXTENSION_KEY] is None:
            return jsonify({"status": "unavailable"}), 503
        return jsonify({"status": "ok"})

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        code = (exc.name or "error").lower().replace(" ", "_")
        return jsonify({"error": code, "message": exc.description}), exc.code or 500

    return app
