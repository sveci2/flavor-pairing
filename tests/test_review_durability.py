"""CP5 tests: reviewed decisions survive reruns and full regeneration
(docs/DATA_FOUNDATION_PLAN.md §7, §13, §16 test_review_durability.py;
docs/DECISIONS.md §J).

The regeneration test proves the decision tables alone (entities +
entity_source_names, exported/imported as CSV) carry every human decision:
a fresh database rebuilt from decision tables + re-ingested synthetic raw
content reproduces identical normalized output, with no reliance on any
private data.
"""

from __future__ import annotations

import pytest

from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_AUTO_MAPPED,
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    create_reviewed_entity,
)
from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.parse.row_parser import parse_source
from flavor_pairing.review.queue import REASON_UNRESOLVED_MAPPING, build_review_queue
from flavor_pairing.store import db
from flavor_pairing.store.csv_io import TABLES, export_table, import_table
from pipeline_helpers import (
    full_config,
    ingest_rows,
    make_clock,
    mapping_row,
    observation_rows,
    run_full,
    seed_entity,
    seed_mapping,
    seed_source,
    table_snapshot,
)

SOURCE = "src_alpha"
DECISION_TABLES = ("sources", "entities", "entity_source_names")


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


def apply_human_remap(connection, source_id, source_text, source_role, entity_id):
    """Simulate a human editing the mapping decision table."""
    connection.execute(
        "UPDATE entity_source_names SET entity_id = ?, normalization_status = ? "
        "WHERE source_id = ? AND source_text = ? AND source_role = ?",
        (entity_id, NORMALIZATION_STATUS_HUMAN_MAPPED, source_id, source_text, source_role),
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Human decisions survive reruns
# ---------------------------------------------------------------------------

def test_human_remap_survives_rerun_and_drives_observations(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_cinnamon", "cinnamon")
    run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)
    assert observation_rows(conn, SOURCE)[0]["paired_entity_id"] == "ent_cinnamon"

    # Human decides this source's 'cinnamon' means a more specific entity.
    create_reviewed_entity(
        conn, entity_id="ent_ceylon", canonical_name="ceylon cinnamon",
        review_status="approved",
    )
    apply_human_remap(conn, SOURCE, "cinnamon", "pairing_entry", "ent_ceylon")
    human_row = tuple(mapping_row(conn, SOURCE, "cinnamon", "pairing_entry"))

    normalize_source(conn, config, SOURCE)

    assert tuple(mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")) == human_row
    (observation,) = observation_rows(conn, SOURCE)
    assert observation["paired_entity_id"] == "ent_ceylon"
    assert observation["normalization_status"] == NORMALIZATION_STATUS_HUMAN_MAPPED


def test_human_resolution_of_unresolved_mapping_fills_paired_entity(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    run_full(conn, config, SOURCE, [("APPLE", "berries, esp. strawberries")], tmp_path, clock)
    (observation,) = observation_rows(conn, SOURCE)
    assert observation["paired_entity_id"] is None

    create_reviewed_entity(
        conn, entity_id="ent_strawberry", canonical_name="strawberry",
        review_status="approved",
    )
    apply_human_remap(
        conn, SOURCE, "berries, esp. strawberries", "pairing_entry", "ent_strawberry"
    )
    normalize_source(conn, config, SOURCE)

    (observation,) = observation_rows(conn, SOURCE)
    assert observation["paired_entity_id"] == "ent_strawberry"
    row = mapping_row(conn, SOURCE, "berries, esp. strawberries", "pairing_entry")
    assert row["normalization_status"] == NORMALIZATION_STATUS_HUMAN_MAPPED


def test_observation_approved_only_when_both_resolutions_are_human(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    create_reviewed_entity(
        conn, entity_id="ent_apple", canonical_name="apple", review_status="approved"
    )
    create_reviewed_entity(
        conn, entity_id="ent_cinnamon", canonical_name="cinnamon", review_status="approved"
    )
    run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)
    # Auto-resolved (exact match) on both sides: stays needs_review.
    assert observation_rows(conn, SOURCE)[0]["review_status"] == "needs_review"

    apply_human_remap(conn, SOURCE, "APPLE", "subject", "ent_apple")
    apply_human_remap(conn, SOURCE, "cinnamon", "pairing_entry", "ent_cinnamon")
    normalize_source(conn, config, SOURCE)

    (observation,) = observation_rows(conn, SOURCE)
    assert observation["review_status"] == "approved"


# ---------------------------------------------------------------------------
# Machine-owned rows may progress; human/unknown rows never change
# ---------------------------------------------------------------------------

def test_machine_unresolved_mapping_becomes_auto_mapped_when_entity_appears(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)
    assert mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")["entity_id"] is None

    # A reviewer creates the entity but does not edit the mapping.
    create_reviewed_entity(
        conn, entity_id="ent_cinnamon", canonical_name="cinnamon", review_status="approved"
    )
    normalize_source(conn, config, SOURCE)

    row = mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")
    assert row["entity_id"] == "ent_cinnamon"
    assert row["normalization_status"] == NORMALIZATION_STATUS_AUTO_MAPPED


def test_human_and_unknown_status_mappings_are_never_overwritten(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_cinnamon", "cinnamon")
    seed_entity(conn, "ent_walnut", "walnut")
    # Human said "unmappable" (null, human_mapped); legacy row has an
    # unrecognized status; both have exact matches available.
    seed_mapping(
        conn, SOURCE, "cinnamon", "pairing_entry",
        entity_id=None, normalization_status=NORMALIZATION_STATUS_HUMAN_MAPPED,
    )
    seed_mapping(
        conn, SOURCE, "WALNUT", "pairing_entry",
        entity_id="ent_walnut", normalization_status="legacy_mapped_status",
    )
    protected_before = [
        tuple(mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")),
        tuple(mapping_row(conn, SOURCE, "WALNUT", "pairing_entry")),
    ]
    entities_before = table_snapshot(conn, "entities")

    run_full(
        conn, config, SOURCE,
        [("APPLE", "cinnamon"), ("APPLE", "WALNUT")],
        tmp_path, clock,
    )

    protected_after = [
        tuple(mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")),
        tuple(mapping_row(conn, SOURCE, "WALNUT", "pairing_entry")),
    ]
    assert protected_before == protected_after
    assert table_snapshot(conn, "entities") == entities_before  # never auto-updated
    by_text = {o["paired_text_raw"]: o for o in observation_rows(conn, SOURCE)}
    assert by_text["cinnamon"]["paired_entity_id"] is None  # human null respected
    assert by_text["WALNUT"]["paired_entity_id"] == "ent_walnut"  # legacy row used as-is


def test_derived_tables_are_rebuilt_discarding_tampering(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_cinnamon", "cinnamon")
    run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)

    conn.execute("UPDATE pairing_observations SET strength_score = 4")  # tamper
    conn.commit()
    normalize_source(conn, config, SOURCE)

    (observation,) = observation_rows(conn, SOURCE)
    assert observation["strength_score"] is None  # rebuilt from parsed evidence


# ---------------------------------------------------------------------------
# Full regeneration from decision tables
# ---------------------------------------------------------------------------

def test_human_mapping_survives_full_regeneration(conn, config, tmp_path, clock):
    rows = [("APPLE", "cinnamon"), ("APPLE", "WALNUT"), ("APPLE", "Season: autumn")]
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_walnut", "walnut")
    run_full(conn, config, SOURCE, rows, tmp_path, clock)

    create_reviewed_entity(
        conn, entity_id="ent_ceylon", canonical_name="ceylon cinnamon",
        review_status="approved",
    )
    apply_human_remap(conn, SOURCE, "cinnamon", "pairing_entry", "ent_ceylon")
    normalize_source(conn, config, SOURCE)
    human_row = tuple(mapping_row(conn, SOURCE, "cinnamon", "pairing_entry"))

    # Export only the decision tables — the durable record of every decision.
    export_dir = tmp_path / "decision_export"
    for table in DECISION_TABLES:
        export_table(conn, TABLES[table], export_dir / TABLES[table].template_filename)

    # Fresh database: decision tables imported, raw re-ingested, rebuilt.
    fresh = db.open_database(":memory:")
    try:
        for table in DECISION_TABLES:
            import_table(fresh, TABLES[table], export_dir / TABLES[table].template_filename)
        ingest_rows(fresh, tmp_path / "regen", SOURCE, rows, make_clock())
        parse_source(fresh, config, SOURCE)
        normalize_source(fresh, config, SOURCE)

        regenerated = tuple(mapping_row(fresh, SOURCE, "cinnamon", "pairing_entry"))
        assert regenerated == human_row  # byte-equivalent human decision
        by_text = {o["paired_text_raw"]: o for o in observation_rows(fresh, SOURCE)}
        assert by_text["cinnamon"]["paired_entity_id"] == "ent_ceylon"

        # Regenerated decision + derived tables match the original database.
        for table in ("entities", "entity_source_names", "entity_attributes",
                      "pairing_observations", "parsed_source_rows"):
            assert table_snapshot(fresh, table) == table_snapshot(conn, table), table

        # And the queue no longer lists the resolved mapping.
        unresolved_keys = [
            item.item_key
            for item in build_review_queue(fresh)
            if item.reason == REASON_UNRESOLVED_MAPPING
        ]
        assert "cinnamon|pairing_entry" not in unresolved_keys
    finally:
        fresh.close()
