"""CP7 tests: sample-package validation (flavor_pairing/validation.py).

The committed data/sample package must pass; every corruption case runs on
a temporary copy and must fail with a specific, attributable error. No
hard-coded sample entity IDs: rows to corrupt are located dynamically.

Note: the committed-package tests assume the one-time
entity_source_names.csv bootstrap migration has been performed (CP7); they
are red against the pre-migration legacy file by design.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from flavor_pairing.validation import (
    CONFIG_FILES,
    DECISION_FILES,
    EXPECTED_FILES,
    GENERATED_FILES,
    validate_sample_package,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"


@pytest.fixture
def package(tmp_path):
    destination = tmp_path / "package"
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
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def errors_of(package_dir: Path):
    return validate_sample_package(package_dir).errors


def assert_error(package_dir: Path, fragment: str):
    errors = errors_of(package_dir)
    assert any(fragment in error for error in errors), (fragment, errors)


# ---------------------------------------------------------------------------
# The committed canonical package
# ---------------------------------------------------------------------------

def test_committed_sample_package_passes():
    result = validate_sample_package(SAMPLE_DIR)
    assert result.ok, result.errors
    assert set(result.counts) == set(EXPECTED_FILES)
    assert all(count >= 0 for count in result.counts.values())


def test_file_role_sets_are_disjoint_and_complete():
    assert set(CONFIG_FILES) | set(DECISION_FILES) | set(GENERATED_FILES) == set(EXPECTED_FILES)
    assert not set(CONFIG_FILES) & set(DECISION_FILES)
    assert not set(CONFIG_FILES) & set(GENERATED_FILES)
    assert not set(DECISION_FILES) & set(GENERATED_FILES)


# ---------------------------------------------------------------------------
# Completeness both ways
# ---------------------------------------------------------------------------

def test_missing_required_file_fails(package):
    (package / "affinity_members.csv").unlink()
    assert_error(package, "affinity_members.csv: required file is missing")


def test_unexpected_stale_csv_fails(package):
    (package / "old_export.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert_error(package, "old_export.csv: unexpected CSV")


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def test_malformed_generated_header_fails(package):
    header, rows = read_rows(package / "entity_attributes.csv")
    header[header.index("attribute_name")] = "attr_name"
    with (package / "entity_attributes.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(list(row.values()))
    assert_error(package, "entity_attributes.csv: header")


def test_config_file_headers_are_checked_exactly(package):
    header, rows = read_rows(package / "attribute_labels.csv")
    header[header.index("attribute_name")] = "normalized_name"
    with (package / "attribute_labels.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(list(row.values()))
    assert_error(package, "attribute_labels.csv: header")


# ---------------------------------------------------------------------------
# Keys and uniqueness
# ---------------------------------------------------------------------------

def test_duplicate_entity_source_names_key_fails(package):
    header, rows = read_rows(package / "entity_source_names.csv")
    clone = dict(rows[0])
    clone["source_name_id"] = "sn_zz_duplicate"
    rows.append(clone)
    write_rows(package / "entity_source_names.csv", header, rows)
    assert_error(package, "entity_source_names.csv")
    assert_error(package, "duplicate key")


def test_committed_mapping_keys_are_unique():
    header, rows = read_rows(SAMPLE_DIR / "entity_source_names.csv")
    keys = [(r["source_id"], r["source_text"], r["source_role"]) for r in rows]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------

def test_broken_paired_entity_foreign_key_fails(package):
    header, rows = read_rows(package / "pairing_observations.csv")
    target = next(row for row in rows if row["paired_entity_id"])
    target["paired_entity_id"] = "ent_does_not_exist"
    write_rows(package / "pairing_observations.csv", header, rows)
    assert_error(package, "unknown paired entity 'ent_does_not_exist'")


def test_orphan_derived_row_fails(package):
    header, rows = read_rows(package / "entity_attributes.csv")
    rows[0]["source_record_id"] = "no_such_record"
    write_rows(package / "entity_attributes.csv", header, rows)
    assert_error(package, "does not trace to any raw_source_rows record")


def test_parent_entity_cycle_fails(package):
    header, rows = read_rows(package / "entities.csv")
    rows[0]["parent_entity_id"] = rows[1]["entity_id"]
    rows[1]["parent_entity_id"] = rows[0]["entity_id"]
    write_rows(package / "entities.csv", header, rows)
    assert_error(package, "parent_entity_id cycle")


# ---------------------------------------------------------------------------
# Affinity member ordering and token fidelity
# ---------------------------------------------------------------------------

def test_member_order_gap_fails(package):
    header, rows = read_rows(package / "affinity_members.csv")
    target = next(row for row in rows if row["member_order"] == "2")
    target["member_order"] = "9"
    write_rows(package / "affinity_members.csv", header, rows)
    assert_error(package, "member_order sequence")


def test_non_integer_member_order_fails(package):
    header, rows = read_rows(package / "affinity_members.csv")
    rows[0]["member_order"] = "second"
    write_rows(package / "affinity_members.csv", header, rows)
    assert_error(package, "is not an integer")


def test_member_token_mismatch_fails(package):
    header, rows = read_rows(package / "affinity_members.csv")
    rows[0]["member_text_raw"] = "tampered token"
    write_rows(package / "affinity_members.csv", header, rows)
    assert_error(package, "do not equal the split of affinity_text_raw")


# ---------------------------------------------------------------------------
# Observation discipline and strength consistency
# ---------------------------------------------------------------------------

def test_observation_for_affinity_row_fails(package):
    _, groups = read_rows(package / "affinity_groups.csv")
    _, entities = read_rows(package / "entities.csv")
    header, observations = read_rows(package / "pairing_observations.csv")
    observations.append({
        "observation_id": "obs_zz_flattened",
        "source_id": groups[0]["source_id"],
        "source_record_id": groups[0]["source_record_id"],
        "subject_entity_id": entities[0]["entity_id"],
        "paired_entity_id": "", "paired_text_raw": "x",
        "strength_label": "", "strength_score": "", "strength_method": "unavailable",
        "normalization_status": "unresolved", "review_status": "needs_review",
    })
    write_rows(package / "pairing_observations.csv", header, observations)
    assert_error(package, "never flattened into pairing observations")


def test_invented_score_on_lossy_plain_row_fails(package):
    parsed_path = package / "parsed_source_rows.csv"
    header, rows = read_rows(parsed_path)
    target = next(
        row for row in rows
        if row["row_type"] == "pairing_candidate" and not row["strength_score"]
        and not row["strength_marker_raw"]
    )
    target["strength_score"] = "2"
    target["strength_label"] = "frequent"
    target["strength_method"] = "typographic_marker"
    write_rows(parsed_path, header, rows)
    assert_error(package, "without marker evidence in typography-lossy format")


def test_observation_strength_must_match_parsed_row(package):
    header, rows = read_rows(package / "pairing_observations.csv")
    target = next(row for row in rows if row["strength_score"])
    original = target["strength_score"]
    target["strength_score"] = "1" if original != "1" else "2"  # always a real change
    write_rows(package / "pairing_observations.csv", header, rows)
    assert_error(package, "strength is copied, never reinterpreted")


# ---------------------------------------------------------------------------
# Status vocabularies: strict for generated, traceable-legacy allowed
# ---------------------------------------------------------------------------

def _relabel_mapping_and_dependents(package, status, *, update_observation):
    """Set a legacy status on a resolved pairing mapping and optionally its
    observation (simulating CP5's verbatim propagation)."""
    map_header, mappings = read_rows(package / "entity_source_names.csv")
    obs_header, observations = read_rows(package / "pairing_observations.csv")
    resolved_texts = {
        row["paired_text_raw"] for row in observations if row["paired_entity_id"]
    }
    mapping = next(
        row for row in mappings
        if row["source_role"] == "pairing_entry" and row["entity_id"]
        and row["source_text"] in resolved_texts
    )
    mapping["normalization_status"] = status
    write_rows(package / "entity_source_names.csv", map_header, mappings)
    if update_observation:
        for row in observations:
            if (
                row["source_id"] == mapping["source_id"]
                and row["paired_text_raw"] == mapping["source_text"]
            ):
                row["normalization_status"] = status
        write_rows(package / "pairing_observations.csv", obs_header, observations)
    return mapping


def test_legacy_mapping_status_copied_into_observation_is_accepted(package):
    _relabel_mapping_and_dependents(package, "legacy_reviewed", update_observation=True)
    assert errors_of(package) == []


def test_untraceable_observation_status_fails(package):
    # The observation invents a status its driving mapping does not carry.
    header, observations = read_rows(package / "pairing_observations.csv")
    target = next(row for row in observations if row["paired_entity_id"])
    target["normalization_status"] = "ghost_status"
    write_rows(package / "pairing_observations.csv", header, observations)
    assert_error(package, "derived rows never invent statuses")


def test_legacy_mapping_status_copied_into_affinity_member_is_accepted(package):
    map_header, mappings = read_rows(package / "entity_source_names.csv")
    mem_header, members = read_rows(package / "affinity_members.csv")
    resolved_member = next(row for row in members if row["member_entity_id"])
    _, groups = read_rows(package / "affinity_groups.csv")
    group = next(
        row for row in groups if row["affinity_id"] == resolved_member["affinity_id"]
    )
    mapping = next(
        row for row in mappings
        if row["source_role"] == "affinity_member"
        and row["source_id"] == group["source_id"]
        and row["source_text"] == resolved_member["member_text_raw"]
    )
    mapping["normalization_status"] = "legacy_member_status"
    resolved_member["normalization_status"] = "legacy_member_status"
    write_rows(package / "entity_source_names.csv", map_header, mappings)
    write_rows(package / "affinity_members.csv", mem_header, members)
    assert errors_of(package) == []


def test_untraceable_member_status_fails(package):
    header, members = read_rows(package / "affinity_members.csv")
    members[0]["normalization_status"] = "ghost_status"
    write_rows(package / "affinity_members.csv", header, members)
    assert_error(package, "derived rows never invent statuses")


def test_strict_review_status_on_generated_rows(package):
    header, rows = read_rows(package / "entity_attributes.csv")
    rows[0]["review_status"] = "maybe_later"
    write_rows(package / "entity_attributes.csv", header, rows)
    assert_error(package, "outside the documented vocabulary")


def test_auto_mapped_row_may_not_reference_rejected_entity(package):
    map_header, mappings = read_rows(package / "entity_source_names.csv")
    mapping = next(
        row for row in mappings
        if row["normalization_status"] == "auto_mapped" and row["entity_id"]
    )
    ent_header, entities = read_rows(package / "entities.csv")
    for row in entities:
        if row["entity_id"] == mapping["entity_id"]:
            row["review_status"] = "rejected"
    write_rows(package / "entities.csv", ent_header, entities)
    assert_error(package, "rejected entities are never auto-selected")


def test_private_path_marker_in_a_cell_fails(package):
    header, rows = read_rows(package / "entity_source_names.csv")
    rows[0]["notes"] = "copied from data/imports_private/secret.csv"
    write_rows(package / "entity_source_names.csv", header, rows)
    assert_error(package, "private path marker")
