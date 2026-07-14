"""CP8 tests: query CLI (scripts/query_flavor.py).

Focused CLI tests; the API itself is covered by test_query_layer.py. All
fixtures are derived dynamically from the committed package or temporary
copies — no hard-coded sample entity IDs or counts.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import pytest

from flavor_pairing.query import FlavorPackage

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_flavor  # noqa: E402  (script module, path-loaded)


@pytest.fixture(scope="module")
def package():
    return FlavorPackage.load(SAMPLE_DIR)


@pytest.fixture(scope="module")
def entity_name(package):
    return next(e.canonical_name for e in package.entities if e.canonical_name)


@pytest.fixture
def package_copy(tmp_path):
    destination = tmp_path / "pkg"
    destination.mkdir()
    for path in SAMPLE_DIR.glob("*.csv"):
        shutil.copyfile(path, destination / path.name)
    return destination


def run_cli(capsys, *argv):
    code = query_flavor.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Successful output
# ---------------------------------------------------------------------------

def test_plain_text_success(capsys, package, entity_name):
    code, out, err = run_cli(capsys, entity_name)
    assert code == 0
    assert err == ""
    entity = package.resolve_entity(entity_name)
    assert f"Entity: {entity.entity_id}" in out
    for heading in (
        "Pairings as subject", "Pairings as paired", "Reverse-pair evidence",
        "Attributes", "Affinity groups as subject", "Affinity groups as member",
        "Unresolved mappings in package", "Unresolved observations in package",
    ):
        assert heading in out


def test_json_success_preserves_null_as_null(capsys, package, entity_name):
    code, out, err = run_cli(capsys, entity_name, "--json")
    assert code == 0 and err == ""
    payload = json.loads(out)
    assert payload["entity"]["entity_id"] == package.resolve_entity(entity_name).entity_id
    # Stored NULLs must be JSON null, never the '-' presentation marker.
    observations = payload["pairings"]["as_subject"] + payload["pairings"]["as_paired"]
    assert observations, "expected observations in JSON output"
    nulls = [o for o in observations if o["strength_score"] is None]
    assert nulls, "expected at least one null strength_score preserved as null"
    assert all(o["strength_score"] != "-" for o in observations)


def test_both_renderings_derive_from_the_same_result(capsys, package, entity_name):
    result = package.query(entity_name)
    code, out, _ = run_cli(capsys, entity_name, "--json")
    assert code == 0
    assert json.loads(out) == query_flavor.result_to_dict(result)

    code, out, _ = run_cli(capsys, entity_name)
    assert code == 0
    assert out.rstrip("\n") == query_flavor.render_text(result)


# ---------------------------------------------------------------------------
# Failure modes and exit codes
# ---------------------------------------------------------------------------

def test_not_found_exits_1(capsys):
    code, out, err = run_cli(capsys, "definitely unknown thing zz")
    assert code == 1
    assert out == ""
    assert "NOT FOUND" in err


def test_ambiguous_name_exits_1(capsys, package_copy):
    path = package_copy / "entities.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames)
        rows = [dict(row) for row in reader]
    original = next(row for row in rows if row["canonical_name"])
    clone = dict(original)
    clone["entity_id"] = "ent_zz_duplicate_name"
    clone["canonical_name"] = original["canonical_name"].upper()
    rows.append(clone)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    code, out, err = run_cli(
        capsys, original["canonical_name"], "--package", str(package_copy)
    )
    assert code == 1
    assert out == ""
    assert "AMBIGUOUS" in err
    assert "ent_zz_duplicate_name" in err


def test_malformed_package_exits_2(capsys, package_copy, entity_name):
    (package_copy / "pairing_observations.csv").unlink()
    code, out, err = run_cli(capsys, entity_name, "--package", str(package_copy))
    assert code == 2
    assert out == ""
    assert "PACKAGE ERROR" in err


# ---------------------------------------------------------------------------
# Section filtering and package path
# ---------------------------------------------------------------------------

def test_section_filtering_text_and_json(capsys, entity_name):
    code, out, _ = run_cli(capsys, entity_name, "--section", "attributes")
    assert code == 0
    assert "Attributes (" in out
    assert "Pairings as subject" not in out
    assert "Affinity groups" not in out

    code, out, _ = run_cli(capsys, entity_name, "--section", "unresolved", "--json")
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {"entity", "unresolved_mappings", "unresolved_observations"}


def test_configurable_package_path(capsys, package_copy, entity_name):
    code, out, _ = run_cli(capsys, entity_name, "--package", str(package_copy))
    assert code == 0
    assert "Entity: " in out


# ---------------------------------------------------------------------------
# Determinism and read-only behavior
# ---------------------------------------------------------------------------

def test_repeated_output_is_deterministic(capsys, entity_name):
    first = run_cli(capsys, entity_name)
    second = run_cli(capsys, entity_name)
    assert first == second
    first_json = run_cli(capsys, entity_name, "--json")
    second_json = run_cli(capsys, entity_name, "--json")
    assert first_json == second_json


def test_cli_never_writes(capsys, package_copy, entity_name):
    before = {path.name: path.read_bytes() for path in package_copy.glob("*.csv")}
    run_cli(capsys, entity_name, "--package", str(package_copy))
    run_cli(capsys, entity_name, "--package", str(package_copy), "--json")
    run_cli(capsys, "definitely unknown thing zz", "--package", str(package_copy))
    after = {path.name: path.read_bytes() for path in package_copy.glob("*.csv")}
    assert before == after
