"""CP6 tests: duplicate and reverse-pair reports (docs/DATA_FOUNDATION_PLAN.md
§8, §16 test_duplicate_detection.py).

Reports are read-only: repeats and reverse pairs are flagged, never merged
or deleted. All fixtures are synthetic; no sample source IDs or fixed
counts.
"""

from __future__ import annotations

import pytest

from flavor_pairing.dupes.detect import (
    duplicate_observation_report,
    raw_text_duplicate_report,
    reverse_pair_report,
)
from pipeline_helpers import (
    full_config,
    make_clock,
    run_full,
    seed_entity,
    seed_source,
    table_snapshot,
)
from flavor_pairing.store import db

SOURCE = "src_alpha"


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def clock():
    return make_clock()


@pytest.fixture
def config(build_config):
    return full_config(build_config)


def seed_pair_entities(connection):
    seed_source(connection, SOURCE)
    for entity_id, name in (
        ("ent_apple", "apple"), ("ent_basil", "basil"), ("ent_cinnamon", "cinnamon"),
    ):
        seed_entity(connection, entity_id, name)


def observation_count(connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) AS n FROM pairing_observations"
    ).fetchone()["n"]


# ---------------------------------------------------------------------------
# Exact duplicate observations
# ---------------------------------------------------------------------------

def test_exact_duplicates_grouped_with_full_provenance(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    # Identical raw rows get distinct content-derived occurrence IDs, so both
    # survive ingestion and both become observations.
    run_full(
        conn, config, SOURCE,
        [("APPLE", "cinnamon"), ("APPLE", "cinnamon"), ("APPLE", "basil")],
        tmp_path, clock,
    )

    report = duplicate_observation_report(conn)
    assert len(report) == 1
    group = report[0]
    assert (group.subject_entity_id, group.paired_entity_id) == ("ent_apple", "ent_cinnamon")
    assert len(group.observations) == 2
    for ref in group.observations:
        assert ref.observation_id and ref.source_id == SOURCE
        assert ref.source_record_id  # provenance retained
        assert ref.paired_text_raw == "cinnamon"
        assert ref.strength_score is None  # plain rows: no invented score
        assert ref.review_status == "needs_review"
    assert observation_count(conn) == 3  # nothing deleted or merged


def test_duplicates_detected_across_sources(conn, build_config, tmp_path, clock):
    other = "src_generated_d802"
    sources = [
        {"source_id": SOURCE, "source_name": "Alpha demo source",
         "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
         "allowed_use": "software_testing"},
        {"source_id": other, "source_name": "Second generated source",
         "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
         "allowed_use": "software_testing"},
    ]
    config = full_config(build_config, **{"sources.csv": sources})
    seed_pair_entities(conn)
    seed_source(conn, other)

    run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)
    run_full(conn, config, other, [("APPLE", "cinnamon")], tmp_path, clock)

    (group,) = duplicate_observation_report(conn)
    assert {ref.source_id for ref in group.observations} == {SOURCE, other}


def test_null_paired_entities_are_never_grouped_as_duplicates(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [("APPLE", "mystery herb"), ("APPLE", "mystery herb")],  # both unresolved
        tmp_path, clock,
    )
    assert duplicate_observation_report(conn) == []


# ---------------------------------------------------------------------------
# Reverse pairs
# ---------------------------------------------------------------------------

def test_reverse_pair_reported_once_with_both_directions_intact(
    conn, config, tmp_path, clock
):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [
            ("APPLE", "BASIL"),   # uppercase: score 3 in this direction
            ("BASIL", "apple"),   # plain: no score in the reverse direction
            ("BASIL", "apple"),   # second observation, same direction
        ],
        tmp_path, clock,
    )

    report = reverse_pair_report(conn)
    assert len(report) == 1
    candidate = report[0]
    assert (candidate.entity_a, candidate.entity_b) == ("ent_apple", "ent_basil")
    assert len(candidate.observations_a_to_b) == 1
    assert len(candidate.observations_b_to_a) == 2
    # Strength evidence stays per-observation: no merging, no symmetry.
    assert candidate.observations_a_to_b[0].strength_score == 3
    assert all(ref.strength_score is None for ref in candidate.observations_b_to_a)
    assert observation_count(conn) == 3


def test_single_direction_is_not_a_reverse_pair(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(conn, config, SOURCE, [("APPLE", "basil")], tmp_path, clock)
    assert reverse_pair_report(conn) == []


def test_self_pair_is_not_a_reverse_pair(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(conn, config, SOURCE, [("APPLE", "apple"), ("APPLE", "apple")], tmp_path, clock)
    assert reverse_pair_report(conn) == []


def test_unresolved_entities_excluded_from_reverse_pairs(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [("APPLE", "mystery herb"), ("BASIL", "apple")],
        tmp_path, clock,
    )
    assert reverse_pair_report(conn) == []


# ---------------------------------------------------------------------------
# Raw-text duplicates
# ---------------------------------------------------------------------------

def test_raw_text_duplicates_group_unresolved_rows_on_exact_text(
    conn, config, tmp_path, clock
):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [
            ("APPLE", "mystery herb"),
            ("APPLE", "mystery herb"),
            ("APPLE", "Mystery Herb"),  # case variant: NOT the same raw text
        ],
        tmp_path, clock,
    )

    report = raw_text_duplicate_report(conn)
    assert len(report) == 1
    group = report[0]
    assert group.subject_entity_id == "ent_apple"
    assert group.paired_text_raw == "mystery herb"  # exact text only
    assert len(group.observations) == 2
    assert all(ref.paired_entity_id is None for ref in group.observations)


def test_resolved_rows_also_appear_in_raw_text_duplicates(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [("APPLE", "cinnamon"), ("APPLE", "cinnamon")],
        tmp_path, clock,
    )
    (group,) = raw_text_duplicate_report(conn)
    assert group.paired_text_raw == "cinnamon"
    assert len(group.observations) == 2


# ---------------------------------------------------------------------------
# Determinism, read-only behavior, generality
# ---------------------------------------------------------------------------

def test_reports_are_deterministic_and_insertion_independent(
    build_config, tmp_path
):
    config = full_config(build_config)
    row_orders = (
        [("APPLE", "cinnamon"), ("APPLE", "cinnamon"), ("APPLE", "BASIL"),
         ("BASIL", "apple"), ("APPLE", "mystery herb"), ("APPLE", "mystery herb")],
        [("APPLE", "mystery herb"), ("BASIL", "apple"), ("APPLE", "cinnamon"),
         ("APPLE", "BASIL"), ("APPLE", "mystery herb"), ("APPLE", "cinnamon")],
    )
    results = []
    for index, rows in enumerate(row_orders):
        connection = db.open_database(":memory:")
        try:
            seed_pair_entities(connection)
            run_full(connection, config, SOURCE, rows, tmp_path / f"o{index}", make_clock())
            results.append(
                (duplicate_observation_report(connection),
                 reverse_pair_report(connection),
                 raw_text_duplicate_report(connection))
            )
            # Rebuilding on the same database is also stable.
            assert duplicate_observation_report(connection) == results[-1][0]
        finally:
            connection.close()
    assert results[0] == results[1]


def test_reports_are_read_only(conn, config, tmp_path, clock):
    seed_pair_entities(conn)
    run_full(
        conn, config, SOURCE,
        [("APPLE", "cinnamon"), ("APPLE", "cinnamon"), ("APPLE", "BASIL"), ("BASIL", "apple")],
        tmp_path, clock,
    )
    tables = ("pairing_observations", "entities", "entity_source_names",
              "parsed_source_rows", "raw_source_rows")
    before = {table: table_snapshot(conn, table) for table in tables}

    duplicate_observation_report(conn)
    reverse_pair_report(conn)
    raw_text_duplicate_report(conn)

    for table in tables:
        assert table_snapshot(conn, table) == before[table], f"{table} was modified"


def test_reports_scale_to_arbitrary_counts(conn, build_config, tmp_path, clock):
    source_id = "src_generated_77b3"
    sources = [{"source_id": source_id, "source_name": "Generated report source",
                "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
                "allowed_use": "software_testing"}]
    config = full_config(build_config, **{"sources.csv": sources})
    seed_source(conn, source_id)
    pair_count, repeat = 9, 3
    for i in range(pair_count):
        seed_entity(conn, f"ent_subj_{i}", f"subject {i}")
        seed_entity(conn, f"ent_pair_{i}", f"partner {i}")
    rows = [
        (f"SUBJECT {i}", f"partner {i}")
        for i in range(pair_count)
        for _ in range(repeat)
    ]
    run_full(conn, config, source_id, rows, tmp_path, clock)

    report = duplicate_observation_report(conn)
    assert len(report) == pair_count
    assert all(len(group.observations) == repeat for group in report)
    assert reverse_pair_report(conn) == []
