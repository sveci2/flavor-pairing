"""CP9 tests: read-only HTTP JSON API (flavor_pairing_api).

Requests go through Flask's test client — no sockets, no network. All
fixtures derive dynamically from the committed sample package or from
temporary copies; no hard-coded sample entity IDs, names, or counts.
Nothing modifies committed files; nothing touches the private data path
(private-path tests build marker-named directories under tmp_path only).
"""

from __future__ import annotations

import ast
import csv
import dataclasses
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from flavor_pairing.query import FlavorPackage
from flavor_pairing.serialization import result_to_dict, to_json_value
from flavor_pairing.validation import PRIVATE_PATH_MARKER
from flavor_pairing_api import create_app
from flavor_pairing_api.app import (
    DEFAULT_PACKAGE_DIR,
    PACKAGE_DIR_ENV_VAR,
    PrivatePackageDirError,
    resolve_package_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
API_DIR = REPO_ROOT / "flavor_pairing_api"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_flavor  # noqa: E402  (script module, path-loaded)

# URL suffix under /api/v1/entities/<name> -> serializer section name.
ENTITY_SECTIONS = {
    "": "all",
    "/pairings": "pairings",
    "/reverse-pairs": "reverse",
    "/attributes": "attributes",
    "/affinities": "affinities",
}


@pytest.fixture(scope="module")
def package():
    return FlavorPackage.load(SAMPLE_DIR)


@pytest.fixture(scope="module")
def entity_name(package):
    return next(e.canonical_name for e in package.entities if e.canonical_name)


@pytest.fixture(scope="module")
def client():
    return create_app(SAMPLE_DIR).test_client()


@pytest.fixture
def package_copy(tmp_path):
    destination = tmp_path / "pkg"
    destination.mkdir()
    for path in SAMPLE_DIR.glob("*.csv"):
        shutil.copyfile(path, destination / path.name)
    return destination


def read_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_rows(path: Path, header, rows) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=header,
            lineterminator="\n",
            restval="",
        )
        writer.writeheader()
        writer.writerows(rows)


def entity_url(name: str, suffix: str = "") -> str:
    return f"/api/v1/entities/{quote(name)}{suffix}"


def package_bytes(package_dir: Path):
    return {path.name: path.read_bytes() for path in package_dir.glob("*.csv")}


# ---------------------------------------------------------------------------
# Health and namespace layout
# ---------------------------------------------------------------------------

def test_health_is_unversioned_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert client.get("/api/v1/health").status_code == 404


def test_data_endpoints_live_only_under_api_v1(client, entity_name):
    for suffix in ENTITY_SECTIONS:
        assert client.get(entity_url(entity_name, suffix)).status_code == 200
    assert client.get("/api/v1/review/unresolved").status_code == 200

    for unversioned in (f"/entities/{quote(entity_name)}", "/review/unresolved"):
        response = client.get(unversioned)
        assert response.status_code == 404
        assert response.get_json()["error"] == "not_found"


def test_unknown_api_route_returns_json_404(client):
    response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"] == "not_found"
    assert "message" in payload


# ---------------------------------------------------------------------------
# One query-and-render path: bodies equal the shared serializer's output
# ---------------------------------------------------------------------------

def test_every_entity_endpoint_matches_result_to_dict(client, package, entity_name):
    result = package.query(entity_name)
    for suffix, section in ENTITY_SECTIONS.items():
        response = client.get(entity_url(entity_name, suffix))
        assert response.status_code == 200, suffix
        assert response.get_json() == result_to_dict(result, section), suffix


def test_api_json_equals_cli_json(capsys, client, entity_name):
    code = query_flavor.main([entity_name, "--json"])
    assert code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert client.get(entity_url(entity_name)).get_json() == cli_payload


def test_null_strength_score_stays_json_null(client, package):
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    blank = next(
        (
            row for row in stored
            if not row["strength_score"]
            and package.entity(row["subject_entity_id"]) is not None
            and package.entity(row["subject_entity_id"]).canonical_name
        ),
        None,
    )
    assert blank is not None, "sample should demonstrate a null-score observation"
    name = package.entity(blank["subject_entity_id"]).canonical_name
    payload = client.get(entity_url(name, "/pairings")).get_json()
    (observation,) = [
        o for o in payload["pairings"]["as_subject"]
        if o["observation_id"] == blank["observation_id"]
    ]
    assert observation["strength_score"] is None  # JSON null, never a marker


# ---------------------------------------------------------------------------
# Entity resolution over HTTP: exact strip().casefold() only
# ---------------------------------------------------------------------------

def test_resolution_variants_resolve_to_the_same_entity(client, package, entity_name):
    expected_id = package.resolve_entity(entity_name).entity_id
    for variant in (entity_name, entity_name.upper(), f"  {entity_name}  "):
        payload = client.get(entity_url(variant)).get_json()
        assert payload["entity"]["entity_id"] == expected_id, variant


def test_unknown_and_non_exact_names_return_404(client, package, entity_name):
    folds = {
        e.canonical_name.strip().casefold()
        for e in package.entities
        if e.canonical_name
    }
    candidates = ["definitely unknown thing zz", f"{entity_name} zz suffix"]
    plural = f"{entity_name}s"
    if plural.casefold() not in folds:  # only a real non-name proves "no plural"
        candidates.append(plural)
    for name in candidates:
        response = client.get(entity_url(name))
        assert response.status_code == 404, name
        payload = response.get_json()
        assert payload["error"] == "entity_not_found"
        assert payload["name"] == name


def test_ambiguous_name_returns_409_with_sorted_candidates(package_copy):
    header, rows = read_rows(package_copy / "entities.csv")
    original = next(row for row in rows if row["canonical_name"])
    clone = dict(original)
    clone["entity_id"] = "ent_zz_duplicate_name"
    clone["canonical_name"] = original["canonical_name"].upper()  # same casefold
    rows.append(clone)
    write_rows(package_copy / "entities.csv", header, rows)

    test_client = create_app(package_copy).test_client()
    response = test_client.get(entity_url(original["canonical_name"]))
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"] == "ambiguous_entity"
    assert payload["name"] == original["canonical_name"]
    assert payload["candidates"] == sorted(payload["candidates"])
    assert {original["entity_id"], "ent_zz_duplicate_name"} <= set(payload["candidates"])


# ---------------------------------------------------------------------------
# Package-wide review surface
# ---------------------------------------------------------------------------

def test_review_unresolved_reports_mappings_and_observations(package_copy):
    header, rows = read_rows(package_copy / "pairing_observations.csv")
    target = next(row for row in rows if row["paired_entity_id"])
    target["paired_entity_id"] = ""  # unresolved observation, raw text kept
    write_rows(package_copy / "pairing_observations.csv", header, rows)

    header, rows = read_rows(package_copy / "entity_source_names.csv")
    rows[0]["entity_id"] = ""  # unresolved mapping
    write_rows(package_copy / "entity_source_names.csv", header, rows)

    expected_package = FlavorPackage.load(package_copy)
    expected = {
        "unresolved_mappings": to_json_value(
            [dataclasses.asdict(m) for m in expected_package.unresolved_mappings()]
        ),
        "unresolved_observations": to_json_value(
            [dataclasses.asdict(o) for o in expected_package.unresolved_observations()]
        ),
    }
    assert expected["unresolved_mappings"] and expected["unresolved_observations"]

    response = create_app(package_copy).test_client().get("/api/v1/review/unresolved")
    assert response.status_code == 200
    assert response.get_json() == expected


# ---------------------------------------------------------------------------
# Startup loading: once per create_app, snapshot never re-read
# ---------------------------------------------------------------------------

def test_package_is_loaded_exactly_once_per_create_app(monkeypatch, entity_name):
    original = FlavorPackage.load
    calls = []

    def counting_load(package_dir):
        calls.append(package_dir)
        return original(package_dir)

    monkeypatch.setattr(FlavorPackage, "load", counting_load)
    test_client = create_app(SAMPLE_DIR).test_client()
    for _ in range(2):
        test_client.get("/health")
        test_client.get(entity_url(entity_name))
        test_client.get("/api/v1/review/unresolved")
    assert len(calls) == 1


def test_responses_come_from_the_startup_snapshot(package_copy, entity_name):
    test_client = create_app(package_copy).test_client()
    before = test_client.get(entity_url(entity_name)).data
    (package_copy / "entities.csv").write_text("garbage", encoding="utf-8")
    assert test_client.get(entity_url(entity_name)).data == before
    assert test_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Degraded startup: 503 everywhere, no filesystem details leaked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("breakage", ["missing_dir", "missing_file"])
def test_unusable_package_serves_503_without_paths(tmp_path, package_copy, breakage):
    if breakage == "missing_dir":
        target = tmp_path / "nowhere"
    else:
        (package_copy / "pairing_observations.csv").unlink()
        target = package_copy

    test_client = create_app(target).test_client()

    health = test_client.get("/health")
    assert health.status_code == 503
    assert health.get_json() == {"status": "unavailable"}

    for url in (entity_url("anything"), "/api/v1/review/unresolved"):
        response = test_client.get(url)
        assert response.status_code == 503
        payload = response.get_json()
        assert payload["error"] == "package_unavailable"
        body_text = response.get_data(as_text=True)
        assert str(tmp_path) not in body_text
        assert str(target) not in body_text


# ---------------------------------------------------------------------------
# Configuration: argument > environment variable > data/sample default
# ---------------------------------------------------------------------------

def test_environment_variable_selects_the_package(monkeypatch, package_copy):
    header, rows = read_rows(package_copy / "pairing_observations.csv")
    target = next(row for row in rows if row["paired_entity_id"])
    target["paired_entity_id"] = ""  # distinguishes the copy from the sample
    write_rows(package_copy / "pairing_observations.csv", header, rows)

    monkeypatch.setenv(PACKAGE_DIR_ENV_VAR, str(package_copy))
    payload = create_app().test_client().get("/api/v1/review/unresolved").get_json()
    assert payload["unresolved_observations"]


def test_explicit_argument_beats_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv(PACKAGE_DIR_ENV_VAR, str(tmp_path / "nowhere"))
    assert create_app(SAMPLE_DIR).test_client().get("/health").status_code == 200


def test_default_package_dir_is_the_committed_sample(monkeypatch):
    monkeypatch.delenv(PACKAGE_DIR_ENV_VAR, raising=False)
    assert resolve_package_dir() == DEFAULT_PACKAGE_DIR.resolve()
    assert create_app().test_client().get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Private-path boundary (marker-named directories under tmp_path only)
# ---------------------------------------------------------------------------

def test_direct_private_path_is_rejected(tmp_path):
    private_pkg = tmp_path / PRIVATE_PATH_MARKER / "pkg"
    private_pkg.mkdir(parents=True)
    with pytest.raises(PrivatePackageDirError):
        resolve_package_dir(private_pkg)
    with pytest.raises(PrivatePackageDirError):
        create_app(private_pkg)


def test_symlink_into_private_path_is_rejected(tmp_path):
    private_root = tmp_path / PRIVATE_PATH_MARKER
    (private_root / "pkg").mkdir(parents=True)
    alias = tmp_path / "published"
    alias.symlink_to(private_root)
    with pytest.raises(PrivatePackageDirError):
        create_app(alias / "pkg")


def test_marker_substring_component_is_not_rejected(tmp_path):
    lookalike = tmp_path / f"not_{PRIVATE_PATH_MARKER}_backup" / "pkg"
    lookalike.mkdir(parents=True)
    for path in SAMPLE_DIR.glob("*.csv"):
        shutil.copyfile(path, lookalike / path.name)
    assert create_app(lookalike).test_client().get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Read-only guarantees and determinism
# ---------------------------------------------------------------------------

def test_get_requests_never_modify_package_files(package_copy, entity_name):
    before = package_bytes(package_copy)
    test_client = create_app(package_copy).test_client()
    test_client.get("/health")
    for suffix in ENTITY_SECTIONS:
        test_client.get(entity_url(entity_name, suffix))
    test_client.get("/api/v1/review/unresolved")
    test_client.get(entity_url("definitely unknown thing zz"))
    assert package_bytes(package_copy) == before


def test_write_methods_are_405_and_modify_nothing(package_copy, entity_name):
    before = package_bytes(package_copy)
    test_client = create_app(package_copy).test_client()
    for url in (entity_url(entity_name), "/api/v1/review/unresolved"):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(test_client, method)(url)
            assert response.status_code == 405, (method, url)
            assert response.get_json()["error"] == "method_not_allowed"
    assert package_bytes(package_copy) == before


def test_bodies_are_deterministic_across_apps_and_requests(package, entity_name):
    first = create_app(SAMPLE_DIR).test_client()
    second = create_app(SAMPLE_DIR).test_client()
    urls = ["/health", "/api/v1/review/unresolved"] + [
        entity_url(entity_name, suffix) for suffix in ENTITY_SECTIONS
    ]
    for url in urls:
        body = first.get(url).data
        assert first.get(url).data == body, url  # repeatable
        assert second.get(url).data == body, url  # app-independent


# ---------------------------------------------------------------------------
# Meta-rules for the API package source
# ---------------------------------------------------------------------------

FORBIDDEN_NETWORK_CLIENT_MODULES = {
    "socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio",
}


def api_source_files():
    files = sorted(API_DIR.rglob("*.py"))
    assert files, "API package should contain Python files"
    return files


def test_api_package_never_references_the_private_path():
    for path in api_source_files():
        assert PRIVATE_PATH_MARKER not in path.read_text(encoding="utf-8"), path


def test_api_package_imports_no_network_client_modules():
    for path in api_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in FORBIDDEN_NETWORK_CLIENT_MODULES, (
                    f"{path} imports network-capable module '{name}'"
                )
