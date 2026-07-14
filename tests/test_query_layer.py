"""CP8 tests: read-only query API (flavor_pairing/query.py).

API-level tests only (the CLI is a later CP8 step). Expected values are
derived dynamically from the committed package's stored tables or built in
temporary copies — no hard-coded sample entity IDs, names, or row counts.
Nothing modifies committed files; the read-only guarantee is
snapshot-verified.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

import pytest

from flavor_pairing.config.loaders import load_config
from flavor_pairing.query import (
    AmbiguousEntityError,
    FlavorPackage,
    QueryError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
INPUT_DIR = REPO_ROOT / "data" / "sample_input"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import regenerate_sample as regen  # noqa: E402  (script module, path-loaded)


@pytest.fixture(scope="module")
def package():
    return FlavorPackage.load(SAMPLE_DIR)


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
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n", restval="")
        writer.writeheader()
        writer.writerows(rows)


def named_entity(package):
    """Any entity with a non-empty canonical name (deterministic pick)."""
    candidates = [e for e in package.entities if e.canonical_name]
    assert candidates, "package should contain named entities"
    return candidates[0]


def folded_names(package):
    return {
        e.canonical_name.strip().casefold()
        for e in package.entities
        if e.canonical_name
    }


# ---------------------------------------------------------------------------
# Entity resolution: strip().casefold() exactly, nothing more
# ---------------------------------------------------------------------------

def test_resolution_trims_and_casefolds(package):
    entity = named_entity(package)
    name = entity.canonical_name
    for variant in (name, f"  {name}  ", name.upper(), f"\t{name.upper()}\n"):
        resolved = package.resolve_entity(variant)
        assert resolved is not None, variant
        assert resolved.entity_id == entity.entity_id, variant


def test_resolution_has_no_fuzzy_or_plural_matching(package):
    entity = named_entity(package)
    name = entity.canonical_name
    folds = folded_names(package)

    plural = f"{name}s"
    if plural.casefold() not in folds:  # only a real non-name proves "no plural"
        assert package.resolve_entity(plural) is None
    truncated = name[:-1]
    if truncated and truncated.casefold() not in folds:
        assert package.resolve_entity(truncated) is None
    assert package.resolve_entity(f"{name} zz suffix") is None  # no substring logic


def test_unknown_name_returns_none(package):
    assert package.resolve_entity("definitely unknown thing zz") is None
    assert package.query("definitely unknown thing zz") is None


def test_ambiguous_folded_name_raises_with_sorted_candidates(package_copy):
    header, rows = read_rows(package_copy / "entities.csv")
    original = next(row for row in rows if row["canonical_name"])
    clone = dict(original)
    clone["entity_id"] = "ent_zz_duplicate_name"
    clone["canonical_name"] = original["canonical_name"].upper()  # same casefold
    rows.append(clone)
    write_rows(package_copy / "entities.csv", header, rows)

    loaded = FlavorPackage.load(package_copy)
    with pytest.raises(AmbiguousEntityError) as excinfo:
        loaded.resolve_entity(original["canonical_name"])
    assert list(excinfo.value.candidates) == sorted(excinfo.value.candidates)
    assert "ent_zz_duplicate_name" in excinfo.value.candidates
    assert original["entity_id"] in excinfo.value.candidates


def test_entity_lookup_by_id(package):
    entity = named_entity(package)
    assert package.entity(entity.entity_id) == entity
    assert package.entity("ent_zz_nonexistent") is None


# ---------------------------------------------------------------------------
# Pairings: two directions, verbatim fields, full provenance
# ---------------------------------------------------------------------------

def test_as_subject_observations_match_stored_rows_exactly(package):
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    assert stored, "sample should contain observations"
    subject_id = stored[0]["subject_entity_id"]
    expected = sorted(
        (row for row in stored if row["subject_entity_id"] == subject_id),
        key=lambda row: row["observation_id"],
    )
    results = package.observations_for(subject_id).as_subject
    assert [r.observation_id for r in results] == [e["observation_id"] for e in expected]
    for result, row in zip(results, expected):
        assert result.source_id == row["source_id"]
        assert result.source_record_id == row["source_record_id"]
        assert result.paired_entity_id == (row["paired_entity_id"] or None)
        assert result.paired_text_raw == (row["paired_text_raw"] or None)
        assert result.strength_label == (row["strength_label"] or None)
        assert result.strength_score == (
            int(row["strength_score"]) if row["strength_score"] else None
        )
        assert result.strength_method == (row["strength_method"] or None)
        assert result.normalization_status == (row["normalization_status"] or None)
        assert result.review_status == (row["review_status"] or None)


def test_stored_null_strength_stays_none(package):
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    blank = next((row for row in stored if not row["strength_score"]), None)
    assert blank is not None, "sample should demonstrate a null-score observation"
    (result,) = [
        o for o in package.observations if o.observation_id == blank["observation_id"]
    ]
    assert result.strength_score is None
    assert result.strength_label == (blank["strength_label"] or None)
    assert result.strength_method == (blank["strength_method"] or None)


def test_directions_are_separate_and_never_merged(package):
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    paired_ids = [row["paired_entity_id"] for row in stored if row["paired_entity_id"]]
    assert paired_ids, "sample should contain resolved paired entities"
    entity_id = paired_ids[0]
    view = package.observations_for(entity_id)
    assert all(o.subject_entity_id == entity_id for o in view.as_subject)
    assert all(o.paired_entity_id == entity_id for o in view.as_paired)
    assert not (
        {o.observation_id for o in view.as_subject}
        & {o.observation_id for o in view.as_paired}
    )


def test_every_observation_is_returned_separately(package):
    total = sum(
        len(package.observations_for(e.entity_id).as_subject) for e in package.entities
    )
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    assert total == len(stored)  # nothing aggregated, nothing dropped


# ---------------------------------------------------------------------------
# Reverse-pair evidence (derived dynamically from the stored table)
# ---------------------------------------------------------------------------

def stored_reverse_pair():
    """One unordered entity pair observed in both directions, from the CSV.

    The canonical sample is expected to demonstrate at least one reverse
    pair (the sample demonstrates every documented evidence path); the
    search itself stays dynamic so no entity IDs are hard-coded.
    """
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    directed = {}
    for row in stored:
        if row["paired_entity_id"]:
            key = (row["subject_entity_id"], row["paired_entity_id"])
            directed.setdefault(key, []).append(row)
    for subject, paired in sorted(directed):
        if subject < paired and (paired, subject) in directed:
            return (subject, paired), directed
    raise AssertionError(
        "committed sample should demonstrate at least one reverse pair"
    )


def test_reverse_pair_evidence_matches_stored_directions(package):
    (entity_a, entity_b), directed = stored_reverse_pair()
    for endpoint in (entity_a, entity_b):
        evidence = [
            e for e in package.reverse_pairs_for(endpoint)
            if (e.entity_a, e.entity_b) == (entity_a, entity_b)
        ]
        assert len(evidence) == 1, endpoint
        (item,) = evidence
        assert [o.observation_id for o in item.observations_a_to_b] == sorted(
            row["observation_id"] for row in directed[(entity_a, entity_b)]
        )
        assert [o.observation_id for o in item.observations_b_to_a] == sorted(
            row["observation_id"] for row in directed[(entity_b, entity_a)]
        )
        # Directions preserved exactly, never combined.
        assert all(
            o.subject_entity_id == entity_a and o.paired_entity_id == entity_b
            for o in item.observations_a_to_b
        )
        assert all(
            o.subject_entity_id == entity_b and o.paired_entity_id == entity_a
            for o in item.observations_b_to_a
        )


def test_single_direction_yields_no_reverse_evidence(package):
    _, stored = read_rows(SAMPLE_DIR / "pairing_observations.csv")
    directed = set()
    for row in stored:
        if row["paired_entity_id"]:
            directed.add((row["subject_entity_id"], row["paired_entity_id"]))
    one_way = next(
        (
            (subject, paired) for subject, paired in sorted(directed)
            if (paired, subject) not in directed and subject != paired
        ),
        None,
    )
    assert one_way is not None, "sample should contain a one-directional pair"
    subject, paired = one_way
    assert all(
        {evidence.entity_a, evidence.entity_b} != {subject, paired}
        for evidence in package.reverse_pairs_for(subject)
    )


def test_self_pairs_are_excluded(package_copy):
    header, rows = read_rows(package_copy / "pairing_observations.csv")
    target = next(row for row in rows if row["paired_entity_id"])
    target["paired_entity_id"] = target["subject_entity_id"]
    write_rows(package_copy / "pairing_observations.csv", header, rows)
    loaded = FlavorPackage.load(package_copy)
    assert all(
        evidence.entity_a != evidence.entity_b
        for evidence in loaded.reverse_pairs_for(target["subject_entity_id"])
    )


# ---------------------------------------------------------------------------
# Attributes and affinities (fixtures located dynamically)
# ---------------------------------------------------------------------------

def test_attributes_with_provenance(package):
    _, stored = read_rows(SAMPLE_DIR / "entity_attributes.csv")
    attributed = [row for row in stored if row["entity_id"]]
    assert attributed, "sample should contain attributed rows"
    entity_id = attributed[0]["entity_id"]
    expected = sorted(
        (row for row in attributed if row["entity_id"] == entity_id),
        key=lambda row: row["attribute_id"],
    )
    results = package.attributes_for(entity_id)
    assert [r.attribute_id for r in results] == [e["attribute_id"] for e in expected]
    for result, row in zip(results, expected):
        assert result.source_id == row["source_id"]
        assert result.source_record_id == row["source_record_id"]
        assert result.attribute_name == (row["attribute_name"] or None)
        assert result.attribute_value_raw == (row["attribute_value_raw"] or None)
        assert result.attribute_value_normalized == (
            row["attribute_value_normalized"] or None
        )


def test_affinities_subject_and_member_views(package):
    _, group_rows = read_rows(SAMPLE_DIR / "affinity_groups.csv")
    _, member_rows = read_rows(SAMPLE_DIR / "affinity_members.csv")
    members_by_group = {}
    for row in member_rows:
        members_by_group.setdefault(row["affinity_id"], []).append(row)
    subject_ids = {row["subject_entity_id"] for row in group_rows if row["subject_entity_id"]}
    member_ids = {
        row["member_entity_id"] for row in member_rows if row["member_entity_id"]
    }

    # One entity that is both the subject and a member of the same group.
    both = next(
        (
            (group, group["subject_entity_id"])
            for group in group_rows
            if group["subject_entity_id"]
            and any(
                m["member_entity_id"] == group["subject_entity_id"]
                for m in members_by_group.get(group["affinity_id"], [])
            )
        ),
        None,
    )
    assert both is not None, "sample should contain a subject-as-member group"
    group_row, subject_id = both
    view = package.affinities_for(subject_id)
    matching = [g for g in view.as_subject if g.affinity_id == group_row["affinity_id"]]
    assert len(matching) == 1
    (group,) = matching
    assert group.affinity_text_raw == (group_row["affinity_text_raw"] or None)
    stored_members = sorted(
        members_by_group[group.affinity_id], key=lambda m: int(m["member_order"])
    )
    assert [m.member_order for m in group.members] == [
        int(m["member_order"]) for m in stored_members
    ]
    assert [m.member_text_raw for m in group.members] == [
        m["member_text_raw"] or None for m in stored_members
    ]
    assert all(m.affinity_id == group.affinity_id for m in group.members)
    assert group in view.as_member  # subject literally appears as a member token

    # One entity that is a member somewhere but the subject of no group.
    member_only = sorted(member_ids - subject_ids)
    assert member_only, "sample should contain a member-only entity"
    member_view = package.affinities_for(member_only[0])
    assert member_view.as_subject == ()
    assert any(
        any(m.member_entity_id == member_only[0] for m in g.members)
        for g in member_view.as_member
    )


def test_affinity_groups_never_appear_as_pairings(package):
    group_records = {
        (g.source_id, g.source_record_id) for g in package.affinity_groups
    }
    observation_records = {
        (o.source_id, o.source_record_id) for o in package.observations
    }
    assert not group_records & observation_records


# ---------------------------------------------------------------------------
# Unresolved sections
# ---------------------------------------------------------------------------

def test_committed_sample_has_no_unresolved_items(package):
    assert package.unresolved_mappings() == ()
    assert package.unresolved_observations() == ()


def test_unresolved_mapping_and_observation_are_reported(tmp_path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    for path in INPUT_DIR.glob("*.csv"):
        shutil.copyfile(path, input_dir / path.name)

    # Select the input file by its configured shape: resolve each file's
    # source format via import_mappings.csv and pick one whose subject and
    # entry columns are present in the file's actual header.
    config = load_config(SAMPLE_DIR)
    selected = None
    for path in sorted(input_dir.glob("*.csv")):
        source = config.sources[path.stem]
        mapping = config.mapping_for(source.source_format)
        subject_column = mapping["subject_raw"].input_column
        entry_column = mapping["entry_raw"].input_column
        header, rows = read_rows(path)
        if subject_column in header and entry_column in header and rows:
            selected = (path, header, rows, subject_column, entry_column)
            break
    assert selected is not None, "no input file offers subject/entry columns"
    path, header, rows, subject_column, entry_column = selected

    # Append a new pairing row reusing an existing subject value from the
    # selected file; 'dragonfruit zest' is the focused unknown-text fixture.
    existing_subject = rows[0][subject_column]
    rows.append({subject_column: existing_subject, entry_column: "dragonfruit zest"})
    write_rows(path, header, rows)

    out = tmp_path / "out"
    report = regen.regenerate(input_dir, SAMPLE_DIR, out)
    assert report.ok, (report.drift, report.validation_errors)

    loaded = FlavorPackage.load(out)
    (mapping_row,) = [
        m for m in loaded.unresolved_mappings() if m.source_text == "dragonfruit zest"
    ]
    assert mapping_row.source_role == "pairing_entry"
    assert mapping_row.normalization_status == "unresolved"
    (observation,) = loaded.unresolved_observations()
    assert observation.paired_entity_id is None
    assert observation.paired_text_raw == "dragonfruit zest"
    # The top-level result carries the package-wide sections too.
    queryable = next(e for e in loaded.entities if e.canonical_name)
    result = loaded.query(queryable.canonical_name)
    assert result.unresolved_mappings == loaded.unresolved_mappings()
    assert result.unresolved_observations == loaded.unresolved_observations()


# ---------------------------------------------------------------------------
# Top-level EntityQueryResult
# ---------------------------------------------------------------------------

def test_query_assembles_every_section_consistently(package):
    entity = named_entity(package)
    result = package.query(entity.canonical_name)
    assert result.entity == entity
    assert result.pairings == package.observations_for(entity.entity_id)
    assert result.reverse_pairs == package.reverse_pairs_for(entity.entity_id)
    assert result.attributes == package.attributes_for(entity.entity_id)
    assert result.affinities == package.affinities_for(entity.entity_id)


# ---------------------------------------------------------------------------
# Determinism and read-only behavior
# ---------------------------------------------------------------------------

def test_results_are_deterministic_across_loads_and_row_order(package, package_copy):
    for name in ("pairing_observations.csv", "affinity_members.csv", "entities.csv"):
        header, rows = read_rows(package_copy / name)
        write_rows(package_copy / name, header, list(reversed(rows)))
    shuffled = FlavorPackage.load(package_copy)
    for entity in package.entities:
        assert shuffled.query(entity.canonical_name) == package.query(entity.canonical_name)


def test_loading_and_querying_is_read_only(package_copy):
    before = {path.name: path.read_bytes() for path in package_copy.glob("*.csv")}
    loaded = FlavorPackage.load(package_copy)
    for entity in loaded.entities:
        loaded.query(entity.canonical_name)
    loaded.unresolved_mappings()
    loaded.unresolved_observations()
    after = {path.name: path.read_bytes() for path in package_copy.glob("*.csv")}
    assert before == after


# ---------------------------------------------------------------------------
# Error behavior
# ---------------------------------------------------------------------------

def test_missing_directory_and_missing_file_raise(tmp_path, package_copy):
    with pytest.raises(QueryError, match=r"package directory not found"):
        FlavorPackage.load(tmp_path / "nowhere")
    (package_copy / "pairing_observations.csv").unlink()
    with pytest.raises(QueryError, match=r"pairing_observations.csv.*missing"):
        FlavorPackage.load(package_copy)


def test_malformed_header_raises(package_copy):
    path = package_copy / "entity_attributes.csv"
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(text.replace("attribute_name", "attr_name", 1), encoding="utf-8")
    with pytest.raises(QueryError, match=r"entity_attributes.csv: header"):
        FlavorPackage.load(package_copy)


def test_ragged_row_raises(package_copy):
    path = package_copy / "entities.csv"
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("ent_zz,name,,unknown,,,,x,EXTRA_CELL\n")
    with pytest.raises(QueryError, match=r"more cells than the header"):
        FlavorPackage.load(package_copy)


def test_duplicate_entity_id_raises(package_copy):
    header, rows = read_rows(package_copy / "entities.csv")
    rows.append(dict(rows[0]))
    write_rows(package_copy / "entities.csv", header, rows)
    with pytest.raises(QueryError, match=r"duplicate entity_id"):
        FlavorPackage.load(package_copy)


def test_orphan_affinity_member_raises(package_copy):
    header, rows = read_rows(package_copy / "affinity_members.csv")
    clone = dict(rows[0])
    clone["affinity_id"] = "aff_zz_orphan"
    rows.append(clone)
    write_rows(package_copy / "affinity_members.csv", header, rows)
    with pytest.raises(QueryError, match=r"unknown affinity_id 'aff_zz_orphan'"):
        FlavorPackage.load(package_copy)


def test_non_integer_member_order_and_score_raise(package_copy, tmp_path):
    header, rows = read_rows(package_copy / "affinity_members.csv")
    rows[0]["member_order"] = "first"
    write_rows(package_copy / "affinity_members.csv", header, rows)
    with pytest.raises(QueryError, match=r"non-integer member_order"):
        FlavorPackage.load(package_copy)

    second = tmp_path / "pkg2"
    second.mkdir()
    for path in SAMPLE_DIR.glob("*.csv"):
        shutil.copyfile(path, second / path.name)
    header, rows = read_rows(second / "pairing_observations.csv")
    target = next(row for row in rows if row["strength_score"])
    target["strength_score"] = "four"
    write_rows(second / "pairing_observations.csv", header, rows)
    with pytest.raises(QueryError, match=r"non-integer strength_score"):
        FlavorPackage.load(second)
