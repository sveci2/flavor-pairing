"""CP5 tests: review-queue report (docs/DATA_FOUNDATION_PLAN.md §7, §16
test_review_queue.py).

The queue is a read-only report; resolution is a decision-table edit
followed by a normalize rerun, after which the queue must shrink by exactly
the resolved items.
"""

from __future__ import annotations

import pytest

from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    create_reviewed_entity,
)
from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.review.queue import (
    QUEUE_TABLES,
    REASON_ENTITY_NEEDS_REVIEW,
    REASON_REQUIRES_REVIEW_ROW,
    REASON_UNCLASSIFIED_ROW,
    REASON_UNRESOLVED_MAPPING,
    REASON_UNRESOLVED_PAIRED,
    build_review_queue,
)
from flavor_pairing.store import db
from pipeline_helpers import (
    full_config,
    make_clock,
    run_full,
    seed_entity,
    seed_mapping,
    seed_source,
    table_snapshot,
)

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


def populated(conn, config, tmp_path, clock):
    """One pipeline run exercising every queue predicate at once."""
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")  # review_status NULL: not queued
    seed_entity(conn, "ent_pending", "pending thing", review_status="needs_review")
    run_full(
        conn, config, SOURCE,
        [
            ("APPLE", "cinnamon"),        # unresolved paired -> mapping + obs items
            ("APPLE", "Mystery: value"),  # unclassified parsed row
            ("APPLE", "apple + cinnamon"),  # affinity_group w/o header: requires_review
        ],
        tmp_path, clock,
    )


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

def test_queue_includes_every_predicate(conn, config, tmp_path, clock):
    populated(conn, config, tmp_path, clock)
    items = build_review_queue(conn)
    by_reason = {}
    for item in items:
        by_reason.setdefault(item.reason, []).append(item)

    assert [i.item_key for i in by_reason[REASON_UNRESOLVED_MAPPING]] == [
        "cinnamon|pairing_entry"
    ]
    assert len(by_reason[REASON_UNCLASSIFIED_ROW]) == 1
    assert "Mystery" in by_reason[REASON_UNCLASSIFIED_ROW][0].detail
    assert len(by_reason[REASON_REQUIRES_REVIEW_ROW]) == 1
    assert "affinity_group" in by_reason[REASON_REQUIRES_REVIEW_ROW][0].detail
    assert [i.item_key for i in by_reason[REASON_ENTITY_NEEDS_REVIEW]] == ["ent_pending"]
    assert len(by_reason[REASON_UNRESOLVED_PAIRED]) == 1
    assert "cinnamon" in by_reason[REASON_UNRESOLVED_PAIRED][0].detail


def test_all_null_mappings_visible_regardless_of_status(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_mapping(conn, SOURCE, "alpha text", "pairing_entry",
                 entity_id=None, normalization_status="unresolved")
    seed_mapping(conn, SOURCE, "beta text", "pairing_entry",
                 entity_id=None, normalization_status=NORMALIZATION_STATUS_HUMAN_MAPPED)
    seed_mapping(conn, SOURCE, "gamma text", "pairing_entry",
                 entity_id=None, normalization_status="mystery_legacy_status")
    seed_mapping(conn, SOURCE, "delta text", "pairing_entry",
                 entity_id=None, normalization_status=None)

    items = [i for i in build_review_queue(conn) if i.reason == REASON_UNRESOLVED_MAPPING]

    assert [i.item_key for i in items] == [
        "alpha text|pairing_entry", "beta text|pairing_entry",
        "delta text|pairing_entry", "gamma text|pairing_entry",
    ]
    details = {i.item_key: i.detail for i in items}
    assert "human_mapped" in details["beta text|pairing_entry"]
    assert "mystery_legacy_status" in details["gamma text|pairing_entry"]
    assert "(none)" in details["delta text|pairing_entry"]


def test_unclassified_rows_are_not_double_listed(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    run_full(conn, config, SOURCE, [("APPLE", "Mystery: value")], tmp_path, clock)

    parsed_items = build_review_queue(conn, table="parsed_source_rows")
    assert len(parsed_items) == 1
    assert parsed_items[0].reason == REASON_UNCLASSIFIED_ROW


# ---------------------------------------------------------------------------
# Ordering, filtering, read-only behavior
# ---------------------------------------------------------------------------

def test_ordering_is_deterministic_and_insertion_independent(
    conn, config, tmp_path, clock
):
    populated(conn, config, tmp_path, clock)
    first = build_review_queue(conn)
    second = build_review_queue(conn)
    assert first == second
    assert first == sorted(
        first, key=lambda i: (i.table, i.reason, i.source_id or "", i.item_key)
    )

    # A separate database with the same content inserted in another order
    # yields the identical queue.
    other = db.open_database(":memory:")
    try:
        seed_source(other, SOURCE)
        seed_entity(other, "ent_pending", "pending thing", review_status="needs_review")
        seed_entity(other, "ent_apple", "apple")
        run_full(
            other, config, SOURCE,
            [
                ("APPLE", "apple + cinnamon"),
                ("APPLE", "Mystery: value"),
                ("APPLE", "cinnamon"),
            ],
            tmp_path / "other", make_clock(),
        )
        assert build_review_queue(other) == first
    finally:
        other.close()


def test_table_filter(conn, config, tmp_path, clock):
    populated(conn, config, tmp_path, clock)
    for table in QUEUE_TABLES:
        for item in build_review_queue(conn, table=table):
            assert item.table == table
    with pytest.raises(ValueError, match=r"unknown review-queue table"):
        build_review_queue(conn, table="raw_source_rows")


def test_queue_is_read_only(conn, config, tmp_path, clock):
    populated(conn, config, tmp_path, clock)
    tables = ("entities", "entity_source_names", "parsed_source_rows",
              "entity_attributes", "pairing_observations")
    before = {table: table_snapshot(conn, table) for table in tables}
    build_review_queue(conn)
    for table in tables:
        assert table_snapshot(conn, table) == before[table], f"{table} was modified"


# ---------------------------------------------------------------------------
# Shrink after resolution
# ---------------------------------------------------------------------------

def test_queue_shrinks_by_exactly_the_resolved_items(conn, config, tmp_path, clock):
    populated(conn, config, tmp_path, clock)
    before = build_review_queue(conn)

    # Human resolves the 'cinnamon' mapping with an approved entity.
    create_reviewed_entity(
        conn, entity_id="ent_cinnamon", canonical_name="cinnamon",
        review_status="approved",
    )
    conn.execute(
        "UPDATE entity_source_names SET entity_id = 'ent_cinnamon', "
        "normalization_status = ? WHERE source_id = ? AND source_text = 'cinnamon' "
        "AND source_role = 'pairing_entry'",
        (NORMALIZATION_STATUS_HUMAN_MAPPED, SOURCE),
    )
    conn.commit()
    normalize_source(conn, config, SOURCE)

    after = build_review_queue(conn)
    before_keys = {(i.reason, i.item_key) for i in before}
    after_keys = {(i.reason, i.item_key) for i in after}
    assert (REASON_UNRESOLVED_MAPPING, "cinnamon|pairing_entry") in before_keys
    assert (REASON_UNRESOLVED_MAPPING, "cinnamon|pairing_entry") not in after_keys
    assert not [k for k in after_keys - before_keys], "nothing new may appear"
    # Exactly the mapping item and the unresolved-paired observation item left.
    gone = before_keys - after_keys
    assert {reason for reason, _ in gone} == {
        REASON_UNRESOLVED_MAPPING, REASON_UNRESOLVED_PAIRED,
    }
    assert len(after) == len(before) - 2
