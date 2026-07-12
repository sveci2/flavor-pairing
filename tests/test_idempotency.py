"""CP5 tests: pipeline idempotency and deterministic regeneration
(docs/DATA_FOUNDATION_PLAN.md §12, §16 test_idempotency.py).

Covers rerunning normalize alone, rerunning the whole
ingest -> parse -> normalize chain, delta-only behavior on a changed source
version, and byte-identical CSV exports across independent databases.
"""

from __future__ import annotations

import pytest

from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.parse.row_parser import parse_source
from flavor_pairing.store import db
from flavor_pairing.store.csv_io import export_all
from pipeline_helpers import (
    full_config,
    ingest_rows,
    make_clock,
    mapping_row,
    run_full,
    seed_entity,
    seed_source,
    table_snapshot,
)

SOURCE = "src_alpha"
OUTPUT_TABLES = ("entities", "entity_source_names", "entity_attributes",
                 "pairing_observations")

ROWS = [
    ("APPLE", "Season: autumn"),
    ("APPLE", "cinnamon"),
    ("APPLE", "WALNUT"),
    ("APPLE", "berries, esp. strawberries"),
]


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


def seed_standard(connection):
    seed_source(connection, SOURCE)
    seed_entity(connection, "ent_apple", "apple")
    seed_entity(connection, "ent_walnut", "walnut")


def snapshots(connection):
    return {table: table_snapshot(connection, table) for table in OUTPUT_TABLES}


def test_normalize_rerun_alone_is_idempotent(conn, config, tmp_path, clock):
    seed_standard(conn)
    run_full(conn, config, SOURCE, ROWS, tmp_path, clock)
    first = snapshots(conn)

    outcome = normalize_source(conn, config, SOURCE)

    assert snapshots(conn) == first
    assert outcome.mappings_created == 0
    assert outcome.mappings_updated == 0


def test_full_pipeline_rerun_is_idempotent(conn, config, tmp_path, clock):
    seed_standard(conn)
    run_full(conn, config, SOURCE, ROWS, tmp_path, clock)
    first = snapshots(conn)

    # Re-ingest identical content, re-parse, re-normalize.
    run_full(conn, config, SOURCE, ROWS, tmp_path, clock)

    assert snapshots(conn) == first
    mapping_count = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_source_names"
    ).fetchone()["n"]
    distinct_keys = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT source_id, source_text, source_role "
        "FROM entity_source_names)"
    ).fetchone()["n"]
    assert mapping_count == distinct_keys  # zero duplicate mappings


def test_changed_version_touches_only_the_delta(conn, config, tmp_path, clock):
    seed_standard(conn)
    run_full(conn, config, SOURCE, ROWS, tmp_path, clock)
    before_mappings = table_snapshot(conn, "entity_source_names")
    before_entities = table_snapshot(conn, "entities")

    run_full(conn, config, SOURCE, ROWS + [("APPLE", "clove")], tmp_path, clock)

    after_mappings = table_snapshot(conn, "entity_source_names")
    assert table_snapshot(conn, "entities") == before_entities
    assert set(before_mappings) <= set(after_mappings)  # nothing lost or altered
    added = set(after_mappings) - set(before_mappings)
    assert len(added) == 1  # exactly the new pairing-entry mapping
    new_row = mapping_row(conn, SOURCE, "clove", "pairing_entry")
    assert new_row["entity_id"] is None
    observation_count = conn.execute(
        "SELECT COUNT(*) AS n FROM pairing_observations"
    ).fetchone()["n"]
    assert observation_count == 4  # 3 previous + clove (subject still resolved)


def test_exports_are_byte_identical_across_independent_runs(
    build_config, tmp_path, clock
):
    config = full_config(build_config)
    export_dirs = []
    for run_index in range(2):
        connection = db.open_database(":memory:")
        try:
            seed_standard(connection)
            run_full(
                connection, config, SOURCE, ROWS,
                tmp_path / f"run{run_index}", make_clock(),
            )
            export_dir = tmp_path / f"export{run_index}"
            export_all(connection, export_dir)
            export_dirs.append(export_dir)
        finally:
            connection.close()

    first_dir, second_dir = export_dirs
    first_files = sorted(path.name for path in first_dir.iterdir())
    assert first_files == sorted(path.name for path in second_dir.iterdir())
    for name in first_files:
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes(), name
