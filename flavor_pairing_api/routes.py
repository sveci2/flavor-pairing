"""Route handlers for the /api/v1 read-only JSON surface (CP9).

Every entity endpoint follows one query-and-render path: resolve through
``FlavorPackage.query`` (exact ``strip().casefold()`` canonical-name
lookup only — no fuzzy, plural, alias, substring, or automatic matching)
and serialize the resulting ``EntityQueryResult`` with the shared
``flavor_pairing.serialization.result_to_dict``, the same serializer the
CLI uses, so HTTP JSON and CLI JSON cannot diverge. Handlers add no
matching, merging, aggregation, scoring, or write behavior of any kind;
stored NULLs reach the client as JSON ``null``.

Error schema (uniform): ``{"error": <machine_code>, "message": <text>,
...extras}``. Unknown entity: 404 ``entity_not_found``. Ambiguous
canonical name: 409 ``ambiguous_entity`` with sorted candidate entity
IDs (the request is well-formed and the entities exist, so neither 400
nor 404 fits; 300 Multiple Choices is a redirection-class status with no
body convention, so 409 Conflict — the identifier conflicts with the
package state by resolving to several resources — is used). Package
unavailable: 503 ``package_unavailable`` with no filesystem detail.
"""

from __future__ import annotations

import dataclasses

from flask import Blueprint, current_app, jsonify

from flavor_pairing.query import AmbiguousEntityError, FlavorPackage
from flavor_pairing.serialization import result_to_dict, to_json_value

# Key under app.extensions holding the FlavorPackage snapshot, or None
# when the package failed to load and the app serves degraded 503s.
EXTENSION_KEY = "flavor_package"

api_v1 = Blueprint("api_v1", __name__)


def error_response(status_code, error, message, **extra):
    payload = {"error": error, "message": message}
    payload.update(extra)
    return jsonify(payload), status_code


@api_v1.before_request
def require_loaded_package():
    if current_app.extensions[EXTENSION_KEY] is None:
        return error_response(
            503, "package_unavailable", "the data package could not be loaded"
        )
    return None


def _package() -> FlavorPackage:
    return current_app.extensions[EXTENSION_KEY]


def _entity_section(name: str, section: str):
    try:
        result = _package().query(name)
    except AmbiguousEntityError as exc:
        return error_response(
            409,
            "ambiguous_entity",
            "several entities share this canonical name after trim+casefold; "
            "disambiguate using the candidate entity IDs",
            name=name,
            candidates=sorted(exc.candidates),
        )
    if result is None:
        return error_response(
            404,
            "entity_not_found",
            "no entity has this canonical name "
            "(exact match after trim+casefold; no fuzzy matching)",
            name=name,
        )
    return jsonify(result_to_dict(result, section))


@api_v1.get("/entities/<name>")
def entity_full(name: str):
    return _entity_section(name, "all")


@api_v1.get("/entities/<name>/pairings")
def entity_pairings(name: str):
    return _entity_section(name, "pairings")


@api_v1.get("/entities/<name>/reverse-pairs")
def entity_reverse_pairs(name: str):
    return _entity_section(name, "reverse")


@api_v1.get("/entities/<name>/attributes")
def entity_attributes(name: str):
    return _entity_section(name, "attributes")


@api_v1.get("/entities/<name>/affinities")
def entity_affinities(name: str):
    return _entity_section(name, "affinities")


@api_v1.get("/review/unresolved")
def review_unresolved():
    package = _package()
    return jsonify(
        {
            "unresolved_mappings": to_json_value(
                [dataclasses.asdict(m) for m in package.unresolved_mappings()]
            ),
            "unresolved_observations": to_json_value(
                [dataclasses.asdict(o) for o in package.unresolved_observations()]
            ),
        }
    )
