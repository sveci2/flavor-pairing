"""CP7 tests: canonical sample regeneration (scripts/regenerate_sample.py).

Every test operates on temporary copies via the script's --input-dir /
--decision-dir / --output options; no committed file is modified. The
committed data/sample package is only read, as the canonical expectation.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
INPUT_DIR = REPO_ROOT / "data" / "sample_input"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import regenerate_sample as regen  # noqa: E402  (script module, path-loaded)

EXPECTED_PACKAGE_FILES = sorted(regen.EXPECTED_FILES)


def copy_package(destination: Path, source: Path = SAMPLE_DIR) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.csv"):
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


def mapping_rows(package_dir: Path):
    return read_rows(package_dir / "entity_source_names.csv")


# ---------------------------------------------------------------------------
# Canonical output and determinism
# ---------------------------------------------------------------------------

def test_regeneration_reproduces_committed_sample(tmp_path):
    report = regen.regenerate(INPUT_DIR, SAMPLE_DIR, tmp_path / "out")
    assert report.ok, (report.drift, report.validation_errors)
    for name in EXPECTED_PACKAGE_FILES:
        assert (tmp_path / "out" / name).read_bytes() == (SAMPLE_DIR / name).read_bytes(), name


def test_two_independent_regenerations_are_byte_identical(tmp_path):
    first = regen.regenerate(INPUT_DIR, SAMPLE_DIR, tmp_path / "one")
    second = regen.regenerate(INPUT_DIR, SAMPLE_DIR, tmp_path / "two")
    assert first.ok and second.ok
    for name in EXPECTED_PACKAGE_FILES:
        assert (tmp_path / "one" / name).read_bytes() == (
            tmp_path / "two" / name
        ).read_bytes(), name


def test_check_mode_passes_and_modifies_nothing():
    before = {
        path.name: path.read_bytes() for path in SAMPLE_DIR.glob("*.csv")
    }
    report = regen.regenerate(INPUT_DIR, SAMPLE_DIR, SAMPLE_DIR, check=True)
    assert report.ok, (report.drift, report.validation_errors)
    after = {path.name: path.read_bytes() for path in SAMPLE_DIR.glob("*.csv")}
    assert before == after


def test_check_mode_detects_content_drift_and_stale_files(tmp_path):
    package = copy_package(tmp_path / "pkg")
    (package / "parsed_source_rows.csv").write_bytes(
        (package / "parsed_source_rows.csv").read_bytes() + b"tampered\n"
    )
    (package / "stale_output.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    report = regen.regenerate(INPUT_DIR, package, package, check=True)

    assert not report.ok
    drift_text = "\n".join(report.drift)
    assert "parsed_source_rows.csv" in drift_text
    assert "stale_output.csv" in drift_text


def test_check_mode_validates_the_destination_package(tmp_path):
    # attribute_labels.csv is not one of the seven exported files, so the
    # byte comparison alone would pass; destination validation must fail.
    package = copy_package(tmp_path / "pkg")
    (package / "attribute_labels.csv").unlink()
    report = regen.regenerate(INPUT_DIR, SAMPLE_DIR, package, check=True)
    assert not report.ok
    assert any("attribute_labels.csv" in error for error in report.validation_errors)


def test_scratch_parent_hosts_a_fresh_cleaned_run_dir(tmp_path):
    scratch_parent = tmp_path / "scratch"
    report = regen.regenerate(
        INPUT_DIR, SAMPLE_DIR, tmp_path / "out", scratch_parent=scratch_parent
    )
    assert report.ok
    assert list(scratch_parent.iterdir()) == []  # run dir removed after success


def test_no_temporary_path_leaks_into_outputs(tmp_path):
    out = tmp_path / "out"
    regen.regenerate(INPUT_DIR, SAMPLE_DIR, out, scratch_parent=tmp_path / "scratch")
    for path in out.glob("*.csv"):
        text = path.read_text(encoding="utf-8-sig")
        assert "flavor_pairing_regen_" not in text, path.name
        assert str(tmp_path) not in text, path.name


def test_private_paths_are_rejected(tmp_path):
    private = tmp_path / "imports_private" / "anything"
    with pytest.raises(regen.RegenError, match=r"imports_private"):
        regen.regenerate(private, SAMPLE_DIR, tmp_path / "out")
    with pytest.raises(regen.RegenError, match=r"imports_private"):
        regen.regenerate(INPUT_DIR, SAMPLE_DIR, private)


# ---------------------------------------------------------------------------
# Reviewed decisions survive; unresolved stays unresolved
# ---------------------------------------------------------------------------

def test_human_decision_survives_regeneration(tmp_path):
    decision_dir = copy_package(tmp_path / "decisions")
    header, entities = read_rows(decision_dir / "entities.csv")
    entities.append({
        "entity_id": "ent_custom_ceylon", "canonical_name": "ceylon cinnamon",
        "display_name": "", "entity_type": "ingredient", "parent_entity_id": "",
        "normalization_status": "human_mapped", "review_status": "approved",
        "notes": "reviewed test decision",
    })
    write_rows(decision_dir / "entities.csv", header, entities)

    map_header, mappings = read_rows(decision_dir / "entity_source_names.csv")
    (human_row,) = [
        row for row in mappings
        if row["source_text"] == "cinnamon" and row["source_role"] == "pairing_entry"
    ]
    human_row["entity_id"] = "ent_custom_ceylon"
    human_row["normalization_status"] = "human_mapped"
    write_rows(decision_dir / "entity_source_names.csv", map_header, mappings)

    out = tmp_path / "out"
    report = regen.regenerate(INPUT_DIR, decision_dir, out)
    assert report.ok, (report.drift, report.validation_errors)

    _, out_mappings = mapping_rows(out)
    (survived,) = [
        row for row in out_mappings
        if row["source_text"] == "cinnamon" and row["source_role"] == "pairing_entry"
    ]
    assert survived == human_row  # byte-equivalent at the data level
    _, observations = read_rows(out / "pairing_observations.csv")
    (cinnamon_obs,) = [
        row for row in observations if row["paired_text_raw"] == "cinnamon"
    ]
    assert cinnamon_obs["paired_entity_id"] == "ent_custom_ceylon"
    assert cinnamon_obs["normalization_status"] == "human_mapped"


def test_unresolved_mapping_stays_unresolved_without_new_entity(tmp_path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    for path in INPUT_DIR.glob("*.csv"):
        shutil.copyfile(path, input_dir / path.name)
    main_file = input_dir / "src_synthetic_main_pairing.csv"
    main_file.write_text(
        main_file.read_text(encoding="utf-8-sig") + "APPLE,dragonfruit zest\n",
        encoding="utf-8",
    )

    out = tmp_path / "out"
    report = regen.regenerate(input_dir, SAMPLE_DIR, out)
    assert report.ok, (report.drift, report.validation_errors)

    _, out_mappings = mapping_rows(out)
    (new_row,) = [
        row for row in out_mappings if row["source_text"] == "dragonfruit zest"
    ]
    assert new_row["entity_id"] == ""
    assert new_row["normalization_status"] == "unresolved"
    # entities.csv is passthrough: byte-identical, nothing invented.
    assert (out / "entities.csv").read_bytes() == (SAMPLE_DIR / "entities.csv").read_bytes()
    _, observations = read_rows(out / "pairing_observations.csv")
    (obs,) = [row for row in observations if row["paired_text_raw"] == "dragonfruit zest"]
    assert obs["paired_entity_id"] == ""


# ---------------------------------------------------------------------------
# One-time legacy-mapping bootstrap
# ---------------------------------------------------------------------------

def _duplicate_first_mapping(decision_dir: Path, **overrides) -> None:
    header, mappings = read_rows(decision_dir / "entity_source_names.csv")
    clone = dict(mappings[0])
    clone["source_name_id"] = "sn_zz_legacy_duplicate"
    clone.update(overrides)
    mappings.append(clone)
    write_rows(decision_dir / "entity_source_names.csv", header, mappings)


def test_default_mode_rejects_duplicated_legacy_mapping_file(tmp_path):
    decision_dir = copy_package(tmp_path / "decisions")
    _duplicate_first_mapping(decision_dir)
    with pytest.raises(regen.RegenError, match=r"bootstrap-legacy-mappings"):
        regen.regenerate(INPUT_DIR, decision_dir, tmp_path / "out")


def test_bootstrap_collapses_identical_duplicates(tmp_path):
    decision_dir = copy_package(tmp_path / "decisions")
    header, original = read_rows(decision_dir / "entity_source_names.csv")
    _duplicate_first_mapping(decision_dir)

    report = regen.regenerate(
        INPUT_DIR, decision_dir, tmp_path / "out", bootstrap_legacy_mappings=True
    )

    assert report.ok, (report.drift, report.validation_errors)
    assert report.bootstrap is not None
    assert report.bootstrap.total_rows == len(original) + 1
    assert report.bootstrap.unique_keys == len(original)
    assert report.bootstrap.duplicates_retired == 1
    _, out_mappings = mapping_rows(tmp_path / "out")
    keys = [(m["source_id"], m["source_text"], m["source_role"]) for m in out_mappings]
    assert len(keys) == len(set(keys))  # all keys unique after migration


def test_bootstrap_aborts_on_conflicting_duplicate_decisions(tmp_path):
    decision_dir = copy_package(tmp_path / "decisions")
    header, entities = read_rows(decision_dir / "entities.csv")
    other_entity = entities[-1]["entity_id"]
    _duplicate_first_mapping(decision_dir, entity_id=other_entity)
    with pytest.raises(regen.RegenError, match=r"disagree on decision-bearing"):
        regen.regenerate(
            INPUT_DIR, decision_dir, tmp_path / "out", bootstrap_legacy_mappings=True
        )


def test_bootstrap_preserves_protected_legacy_status(tmp_path):
    decision_dir = copy_package(tmp_path / "decisions")
    header, mappings = read_rows(decision_dir / "entity_source_names.csv")
    (legacy_row,) = [
        row for row in mappings
        if row["source_text"] == "cinnamon" and row["source_role"] == "pairing_entry"
    ]
    legacy_row["normalization_status"] = "legacy_reviewed_mapping"
    write_rows(decision_dir / "entity_source_names.csv", header, mappings)

    report = regen.regenerate(
        INPUT_DIR, decision_dir, tmp_path / "out", bootstrap_legacy_mappings=True
    )

    assert report.ok, (report.drift, report.validation_errors)
    assert report.bootstrap.preserved_statuses.get("legacy_reviewed_mapping") == 1
    _, out_mappings = mapping_rows(tmp_path / "out")
    (survived,) = [
        row for row in out_mappings
        if row["source_text"] == "cinnamon" and row["source_role"] == "pairing_entry"
    ]
    assert survived["normalization_status"] == "legacy_reviewed_mapping"
    assert survived["entity_id"] == legacy_row["entity_id"]


def test_committed_sample_regenerates_without_bootstrap_flag(tmp_path):
    """After the one-time migration, ordinary strict regeneration suffices."""
    report = regen.regenerate(INPUT_DIR, SAMPLE_DIR, tmp_path / "out")
    assert report.ok
    assert report.bootstrap is None
