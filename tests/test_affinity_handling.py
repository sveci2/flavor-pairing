"""CP6 tests: derived affinity groups/members (docs/DECISIONS.md §B;
docs/DATA_FOUNDATION_PLAN.md §9, §16 test_affinity_handling.py).

All sources and entities are synthetic/generated; no sample source IDs, no
fixed sample counts; ledger roots are tmp_path-based only.
"""

from __future__ import annotations

import pytest

from flavor_pairing.config.loaders import ConfigError
from flavor_pairing.normalize.affinities import normalize_affinities
from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_AUTO_MAPPED,
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    NormalizeError,
    create_reviewed_entity,
)
from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.parse.row_parser import parse_source
from flavor_pairing.review.queue import (
    REASON_UNRESOLVED_AFFINITY_MEMBER,
    REASON_UNRESOLVED_AFFINITY_SUBJECT,
    build_review_queue,
)
from flavor_pairing.store import db
from pipeline_helpers import (
    full_config,
    group_rows,
    ingest_rows,
    make_clock,
    mapping_row,
    member_rows,
    observation_rows,
    run_full,
    run_full_with_affinities,
    seed_entity,
    seed_mapping,
    seed_source,
    table_snapshot,
)

SOURCE = "src_alpha"

# conftest's approved rule for fmt_alpha: header "Combinations", delimiter " + ".
AFFINITY_ROWS = [
    ("APPLE", "Combinations"),
    ("APPLE", "apple + cinnamon + walnut"),
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


def seed_basics(connection):
    seed_source(connection, SOURCE)
    for entity_id, name in (
        ("ent_apple", "apple"), ("ent_cinnamon", "cinnamon"), ("ent_walnut", "walnut"),
    ):
        seed_entity(connection, entity_id, name)


def entity_count(connection) -> int:
    return connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]


# ---------------------------------------------------------------------------
# Group reconstruction
# ---------------------------------------------------------------------------

def test_group_reconstruction_order_and_raw_text(conn, config, tmp_path, clock):
    seed_basics(conn)
    outcome = run_full_with_affinities(conn, config, SOURCE, AFFINITY_ROWS, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    assert group["affinity_text_raw"] == "apple + cinnamon + walnut"  # verbatim
    assert group["subject_entity_id"] == "ent_apple"
    assert group["review_status"] == "needs_review"  # auto-resolved, not human
    members = member_rows(conn, group["affinity_id"])
    assert [m["member_order"] for m in members] == [1, 2, 3]
    assert [m["member_text_raw"] for m in members] == ["apple", "cinnamon", "walnut"]
    assert [m["member_entity_id"] for m in members] == [
        "ent_apple", "ent_cinnamon", "ent_walnut"
    ]
    assert all(
        m["normalization_status"] == NORMALIZATION_STATUS_AUTO_MAPPED for m in members
    )
    assert outcome.groups_written == 1
    assert outcome.members_written == 3
    assert outcome.unresolved_members == 0
    # Provenance: the group traces to a real current-version raw row.
    raw = conn.execute(
        "SELECT entry_raw FROM raw_source_rows WHERE source_id = ? AND source_record_id = ?",
        (SOURCE, group["source_record_id"]),
    ).fetchone()
    assert raw["entry_raw"] == group["affinity_text_raw"]


def test_subject_token_is_a_member_only_when_literally_present(conn, config, tmp_path, clock):
    seed_basics(conn)
    rows = AFFINITY_ROWS + [("TOMATO", "Combinations"), ("TOMATO", "cinnamon + walnut")]
    seed_entity(conn, "ent_tomato", "tomato")
    run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    groups = {g["affinity_text_raw"]: g for g in group_rows(conn, SOURCE)}
    apple_members = member_rows(conn, groups["apple + cinnamon + walnut"]["affinity_id"])
    assert apple_members[0]["member_entity_id"] == "ent_apple"  # subject as member 1
    tomato_members = member_rows(conn, groups["cinnamon + walnut"]["affinity_id"])
    # Subject 'tomato' is not a token: never synthetically added as a member.
    assert [m["member_text_raw"] for m in tomato_members] == ["cinnamon", "walnut"]


def test_members_resolve_under_affinity_member_role(conn, config, tmp_path, clock):
    seed_basics(conn)
    rows = [("APPLE", "cinnamon")] + AFFINITY_ROWS  # same string, both roles
    run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    as_member = mapping_row(conn, SOURCE, "cinnamon", "affinity_member")
    as_entry = mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")
    assert as_member is not None and as_entry is not None
    assert as_member["source_name_id"] != as_entry["source_name_id"]  # distinct keys
    assert as_member["entity_id"] == as_entry["entity_id"] == "ent_cinnamon"


def test_member_matching_is_trim_and_case_fold_only(conn, config, tmp_path, clock):
    seed_basics(conn)
    rows = [
        ("APPLE", "Combinations"),
        ("APPLE", "apple +  CINNAMON + walnuts"),  # padded-case token; plural token
    ]
    run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    members = {m["member_text_raw"]: m for m in member_rows(conn, group["affinity_id"])}
    assert set(members) == {"apple", " CINNAMON", "walnuts"}  # raw tokens exact
    assert members[" CINNAMON"]["member_entity_id"] == "ent_cinnamon"  # trim+fold match
    assert members["walnuts"]["member_entity_id"] is None  # no plural matching
    assert members["walnuts"]["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED
    assert entity_count(conn) == 3  # nothing created


def test_empty_token_member_kept_without_blank_mapping_row(conn, config, tmp_path, clock):
    seed_basics(conn)
    rows = [("APPLE", "Combinations"), ("APPLE", "apple +  + walnut")]
    outcome = run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    members = member_rows(conn, group["affinity_id"])
    assert [m["member_text_raw"] for m in members] == ["apple", "", "walnut"]
    empty = members[1]
    assert empty["member_entity_id"] is None
    assert empty["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED
    # No blank entity_source_names row was created for the empty token.
    blank_mappings = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_source_names WHERE TRIM(source_text) = ''"
    ).fetchone()["n"]
    assert blank_mappings == 0
    assert group["review_status"] == "needs_review"
    assert outcome.unresolved_members == 1
    keys = {
        (i.reason, i.item_key) for i in build_review_queue(conn, table="affinity_members")
    }
    assert (REASON_UNRESOLVED_AFFINITY_MEMBER, f"{group['affinity_id']}|0002") in keys


def test_unresolved_member_stays_null_without_placeholder(conn, config, tmp_path, clock):
    seed_basics(conn)
    rows = [("APPLE", "Combinations"), ("APPLE", "apple + saffron threads")]
    outcome = run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    members = member_rows(conn, group["affinity_id"])
    assert members[1]["member_entity_id"] is None
    assert entity_count(conn) == 3  # no placeholder entity
    row = mapping_row(conn, SOURCE, "saffron threads", "affinity_member")
    assert row["entity_id"] is None
    assert row["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED
    assert outcome.unresolved_members == 1


def test_rejected_entity_excluded_from_member_matching(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_walnut", "walnut", review_status="rejected")
    rejected_before = tuple(
        conn.execute("SELECT * FROM entities WHERE entity_id = 'ent_walnut'").fetchone()
    )

    rows = [("APPLE", "Combinations"), ("APPLE", "apple + walnut")]
    run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    members = member_rows(conn, group["affinity_id"])
    assert members[1]["member_entity_id"] is None  # never mapped to rejected
    assert entity_count(conn) == 2  # never recreated
    rejected_after = tuple(
        conn.execute("SELECT * FROM entities WHERE entity_id = 'ent_walnut'").fetchone()
    )
    assert rejected_before == rejected_after


def test_unresolved_subject_group_written_counted_and_queued(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_cinnamon", "cinnamon")
    rows = [("MYSTERY ROOT", "Combinations"), ("MYSTERY ROOT", "cinnamon + saffron")]
    outcome = run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)

    (group,) = group_rows(conn, SOURCE)
    assert group["subject_entity_id"] is None  # written, not skipped
    assert group["review_status"] == "needs_review"
    assert outcome.groups_with_unresolved_subject == 1
    keys = {
        (i.reason, i.item_key) for i in build_review_queue(conn, table="affinity_groups")
    }
    assert (REASON_UNRESOLVED_AFFINITY_SUBJECT, group["affinity_id"]) in keys


# ---------------------------------------------------------------------------
# Affinities never become binary pairings
# ---------------------------------------------------------------------------

def test_affinity_rows_produce_no_pairing_observations(conn, config, tmp_path, clock):
    seed_basics(conn)
    run_full_with_affinities(conn, config, SOURCE, AFFINITY_ROWS, tmp_path, clock)
    assert observation_rows(conn, SOURCE) == []


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------

def test_human_member_mapping_survives_rerun_and_gates_approval(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    create_reviewed_entity(conn, entity_id="ent_apple", canonical_name="apple",
                           review_status="approved")
    create_reviewed_entity(conn, entity_id="ent_cinnamon", canonical_name="cinnamon",
                           review_status="approved")
    rows = [("APPLE", "Combinations"), ("APPLE", "apple + cinnamon")]
    run_full_with_affinities(conn, config, SOURCE, rows, tmp_path, clock)
    (group,) = group_rows(conn, SOURCE)
    assert group["review_status"] == "needs_review"  # auto-resolved only

    # Human marks the subject and one member; group must stay needs_review.
    for text, role in (("APPLE", "subject"), ("apple", "affinity_member")):
        conn.execute(
            "UPDATE entity_source_names SET normalization_status = ? "
            "WHERE source_id = ? AND source_text = ? AND source_role = ?",
            (NORMALIZATION_STATUS_HUMAN_MAPPED, SOURCE, text, role),
        )
    conn.commit()
    normalize_affinities(conn, config, SOURCE)
    (group,) = group_rows(conn, SOURCE)
    assert group["review_status"] == "needs_review"  # 'cinnamon' still machine-owned

    # Human completes the last member: now every driving decision is human.
    conn.execute(
        "UPDATE entity_source_names SET normalization_status = ? "
        "WHERE source_id = ? AND source_text = 'cinnamon' AND source_role = 'affinity_member'",
        (NORMALIZATION_STATUS_HUMAN_MAPPED, SOURCE),
    )
    conn.commit()
    human_row = tuple(mapping_row(conn, SOURCE, "cinnamon", "affinity_member"))
    normalize_affinities(conn, config, SOURCE)

    assert tuple(mapping_row(conn, SOURCE, "cinnamon", "affinity_member")) == human_row
    (group,) = group_rows(conn, SOURCE)
    assert group["review_status"] == "approved"
    members = member_rows(conn, group["affinity_id"])
    assert all(
        m["normalization_status"] == NORMALIZATION_STATUS_HUMAN_MAPPED for m in members
    )


def test_decision_tables_untouched_by_affinity_rebuild(conn, config, tmp_path, clock):
    seed_basics(conn)
    seed_mapping(conn, SOURCE, "walnut", "affinity_member",
                 entity_id="ent_walnut", normalization_status="legacy_status")
    entities_before = table_snapshot(conn, "entities")
    protected_before = tuple(mapping_row(conn, SOURCE, "walnut", "affinity_member"))

    run_full_with_affinities(conn, config, SOURCE, AFFINITY_ROWS, tmp_path, clock)

    assert table_snapshot(conn, "entities") == entities_before
    assert tuple(mapping_row(conn, SOURCE, "walnut", "affinity_member")) == protected_before
    (group,) = group_rows(conn, SOURCE)
    members = member_rows(conn, group["affinity_id"])
    assert members[2]["member_entity_id"] == "ent_walnut"  # legacy row used as-is
    assert members[2]["normalization_status"] == "legacy_status"  # copied verbatim


# ---------------------------------------------------------------------------
# Determinism, idempotency, versioning
# ---------------------------------------------------------------------------

def test_deterministic_ids_across_fresh_databases(build_config, tmp_path, clock):
    config = full_config(build_config)
    snapshots = []
    for run_index in range(2):
        connection = db.open_database(":memory:")
        try:
            seed_source(connection, SOURCE)
            for entity_id, name in (("ent_apple", "apple"), ("ent_cinnamon", "cinnamon"),
                                    ("ent_walnut", "walnut")):
                seed_entity(connection, entity_id, name)
            run_full_with_affinities(
                connection, config, SOURCE, AFFINITY_ROWS,
                tmp_path / f"run{run_index}", make_clock(),
            )
            snapshots.append(
                (table_snapshot(connection, "affinity_groups"),
                 table_snapshot(connection, "affinity_members"))
            )
        finally:
            connection.close()
    assert snapshots[0] == snapshots[1]


def test_rerun_is_idempotent_and_rebuild_discards_tampering(conn, config, tmp_path, clock):
    seed_basics(conn)
    run_full_with_affinities(conn, config, SOURCE, AFFINITY_ROWS, tmp_path, clock)
    first = (table_snapshot(conn, "affinity_groups"), table_snapshot(conn, "affinity_members"))

    normalize_affinities(conn, config, SOURCE)
    assert (table_snapshot(conn, "affinity_groups"),
            table_snapshot(conn, "affinity_members")) == first

    conn.execute("UPDATE affinity_members SET member_text_raw = 'tampered'")
    conn.commit()
    normalize_affinities(conn, config, SOURCE)
    assert (table_snapshot(conn, "affinity_groups"),
            table_snapshot(conn, "affinity_members")) == first


def test_changed_version_affects_only_this_source(conn, build_config, tmp_path, clock):
    other = "src_generated_af52"
    sources = [
        {"source_id": SOURCE, "source_name": "Alpha demo source",
         "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
         "allowed_use": "software_testing"},
        {"source_id": other, "source_name": "Second generated source",
         "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
         "allowed_use": "software_testing"},
    ]
    config = full_config(build_config, **{"sources.csv": sources})
    seed_source(conn, SOURCE)
    seed_source(conn, other)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_tomato", "tomato")

    run_full_with_affinities(conn, config, SOURCE, AFFINITY_ROWS, tmp_path, clock)
    run_full_with_affinities(
        conn, config, other,
        [("TOMATO", "Combinations"), ("TOMATO", "tomato + basil")],
        tmp_path, clock,
    )
    other_before = [tuple(r) for r in group_rows(conn, other)]

    # New version for SOURCE only: a different affinity phrase.
    run_full_with_affinities(
        conn, config, SOURCE,
        [("APPLE", "Combinations"), ("APPLE", "apple + clove")],
        tmp_path, clock,
    )

    assert [tuple(r) for r in group_rows(conn, other)] == other_before
    (group,) = group_rows(conn, SOURCE)  # old group replaced, not accumulated
    assert group["affinity_text_raw"] == "apple + clove"


def test_arbitrary_source_ids_and_row_counts(conn, build_config, tmp_path, clock):
    source_id = "src_generated_9d17"
    sources = [{"source_id": source_id, "source_name": "Generated affinity source",
                "source_format": "fmt_alpha", "rights_status": "project_owned_demo",
                "allowed_use": "software_testing"}]
    config = full_config(build_config, **{"sources.csv": sources})
    seed_source(conn, source_id)

    group_count = 13
    rows = []
    for i in range(group_count):
        rows.append((f"SUBJECT {i}", "Combinations"))
        rows.append((f"SUBJECT {i}", f"item {i}a + item {i}b + item {i}c"))
    outcome = run_full_with_affinities(conn, config, source_id, rows, tmp_path, clock)

    assert outcome.groups_written == group_count
    assert outcome.members_written == group_count * 3
    assert outcome.unresolved_members == group_count * 3  # nothing seeded, all NULL


# ---------------------------------------------------------------------------
# Preconditions and rule enforcement
# ---------------------------------------------------------------------------

def test_unapproved_rule_fails_fast_with_no_partial_writes(
    conn, config, build_config, tmp_path, clock
):
    seed_basics(conn)
    ingest_rows(conn, tmp_path, SOURCE, AFFINITY_ROWS, clock)
    parse_source(conn, config, SOURCE)  # parsed with the approved rule

    pending_rules = [{
        "source_format": "fmt_alpha", "affinity_header_phrase": "Combinations",
        "member_delimiter": " + ", "review_status": "needs_review",
    }]
    pending_config = full_config(build_config, **{"affinity_split_rules.csv": pending_rules})
    with pytest.raises(ConfigError, match=r"only approved rules"):
        normalize_affinities(conn, pending_config, SOURCE)

    assert group_rows(conn, SOURCE) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM affinity_members").fetchone()["n"] == 0


def test_preconditions(conn, config, tmp_path, clock):
    seed_basics(conn)
    with pytest.raises(NormalizeError, match=r"no completed import run"):
        normalize_affinities(conn, config, SOURCE)
    ingest_rows(conn, tmp_path, SOURCE, AFFINITY_ROWS, clock)
    with pytest.raises(NormalizeError, match=r"no parsed rows"):
        normalize_affinities(conn, config, SOURCE)
    parse_source(conn, config, SOURCE)
    normalize_affinities(conn, config, SOURCE)  # fine now
    ingest_rows(conn, tmp_path, SOURCE, [("APPLE", "clove")], clock)  # new version
    with pytest.raises(NormalizeError, match=r"re-run the parser"):
        normalize_affinities(conn, config, SOURCE)


# ---------------------------------------------------------------------------
# CP5 review-queue regression
# ---------------------------------------------------------------------------

def test_queue_without_affinity_rows_is_unchanged_by_cp6(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    run_full(conn, config, SOURCE, [("APPLE", "cinnamon"), ("APPLE", "Mystery: x")],
             tmp_path, clock)
    before = build_review_queue(conn)

    normalize_affinities(conn, config, SOURCE)  # no affinity rows: writes nothing

    assert build_review_queue(conn) == before
    assert group_rows(conn, SOURCE) == []
