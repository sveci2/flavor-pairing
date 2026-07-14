"""CP2 tests: schema initialization, foreign keys, and constraints.

Covers ``flavor_pairing/store/schema.sql`` and ``store/db.py`` — table
existence, foreign-key enforcement (rejecting orphaned references and
accepting documented-nullable ones), unique/natural-key constraints, and the
project's portability rules (no AUTOINCREMENT, no PRAGMA outside db.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flavor_pairing.store import TABLES, db


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


def _table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return {row["name"] for row in rows}


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------

LEDGER_TABLES = {"import_runs", "run_rows"}


def test_all_fifteen_tables_created(conn):
    # The 13 CP2 tables (TABLES) plus the two CP3A ledger tables, which are
    # deliberately not part of TABLES/csv_io's templated round-trip metadata
    # (docs/DATA_FOUNDATION_PLAN.md §3: the ledger has its own append-only,
    # per-source_id convention, not the single-directory template convention).
    assert _table_names(conn) == set(TABLES) | LEDGER_TABLES


def test_initialize_schema_is_idempotent(conn):
    db.initialize_schema(conn)  # second call must not raise
    assert _table_names(conn) == set(TABLES) | LEDGER_TABLES


def test_derived_aggregate_table_not_created(conn):
    # pairing_edges (derived aggregate, out of scope this phase) must not
    # appear. import_runs/run_rows (CP3A ledger) now do.
    assert "pairing_edges" not in _table_names(conn)
    assert LEDGER_TABLES <= _table_names(conn)


# ---------------------------------------------------------------------------
# Foreign-key enforcement: rejected references
# ---------------------------------------------------------------------------

def test_foreign_keys_enforcement_is_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_raw_source_rows_rejects_unknown_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_source_rows "
            "(source_id, source_record_id, source_order, subject_raw, entry_raw) "
            "VALUES ('src_missing', 'src_missing:x:1', 1, 'A', 'B')"
        )


def test_parsed_source_rows_rejects_row_without_matching_raw_row(conn):
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO parsed_source_rows "
            "(source_id, source_record_id, row_type, parser_confidence, requires_review) "
            "VALUES ('src_a', 'src_a:none:1', 'pairing_candidate', 'high', 0)"
        )


def test_entities_self_reference_rejects_unknown_parent(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entities (entity_id, parent_entity_id) VALUES ('ent_1', 'ent_missing')"
        )


def test_pairing_observations_rejects_unknown_subject_entity(conn):
    _seed_source_and_raw_row(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pairing_observations "
            "(observation_id, source_id, source_record_id, subject_entity_id) "
            "VALUES ('obs_1', 'src_a', 'src_a:r1:1', 'ent_missing')"
        )


def test_affinity_members_rejects_unknown_affinity_group(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO affinity_members (affinity_id, member_order) VALUES ('aff_missing', 1)"
        )


def test_import_runs_rejects_unknown_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO import_runs (run_id, source_id, started_at, status) "
            "VALUES ('run_1', 'src_missing', '2026-01-01T00:00:00+00:00', 'completed')"
        )


def test_import_runs_status_check_constraint_rejects_invalid_value(conn):
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO import_runs (run_id, source_id, started_at, status) "
            "VALUES ('run_1', 'src_a', '2026-01-01T00:00:00+00:00', 'bogus')"
        )


def test_run_rows_rejects_unknown_run(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_rows (run_id, source_record_id, source_order) "
            "VALUES ('run_missing', 'src_a:hash:1', 1)"
        )


def test_run_rows_rejects_unknown_source_record_id(conn):
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    conn.execute(
        "INSERT INTO import_runs (run_id, source_id, started_at, status) "
        "VALUES ('run_1', 'src_a', '2026-01-01T00:00:00+00:00', 'in_progress')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_rows (run_id, source_record_id, source_order) "
            "VALUES ('run_1', 'src_a:missing:1', 1)"
        )


def test_raw_source_rows_source_record_id_unique_across_sources(conn):
    # source_record_id already embeds source_id (docs/DECISIONS.md §H), so a
    # standalone UNIQUE constraint (independent of the composite PRIMARY KEY)
    # is expected, not incidental — it's what lets run_rows reference
    # source_record_id alone.
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_b', 'B', 'fmt_a', 'project_owned_demo')"
    )
    conn.execute(
        "INSERT INTO raw_source_rows "
        "(source_id, source_record_id, source_order, subject_raw, entry_raw) "
        "VALUES ('src_a', 'dup_id', 1, 'X', 'Y')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_source_rows "
            "(source_id, source_record_id, source_order, subject_raw, entry_raw) "
            "VALUES ('src_b', 'dup_id', 1, 'X', 'Y')"
        )


def _seed_source_and_raw_row(connection):
    connection.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    connection.execute(
        "INSERT INTO raw_source_rows "
        "(source_id, source_record_id, source_order, subject_raw, entry_raw) "
        "VALUES ('src_a', 'src_a:r1:1', 1, 'APPLE', 'cinnamon')"
    )


# ---------------------------------------------------------------------------
# Foreign-key enforcement: approved-nullable references accepted
# ---------------------------------------------------------------------------

def test_entity_attributes_entity_id_nullable(conn):
    _seed_source_and_raw_row(conn)
    conn.execute(
        "INSERT INTO entity_attributes "
        "(attribute_id, source_id, source_record_id, entity_id, attribute_name, attribute_value_raw) "
        "VALUES ('attr_1', 'src_a', 'src_a:r1:1', NULL, 'season', 'fall')"
    )
    row = conn.execute("SELECT entity_id FROM entity_attributes WHERE attribute_id = 'attr_1'").fetchone()
    assert row["entity_id"] is None


def test_affinity_groups_subject_entity_id_nullable(conn):
    _seed_source_and_raw_row(conn)
    conn.execute(
        "INSERT INTO affinity_groups "
        "(affinity_id, source_id, source_record_id, subject_entity_id, affinity_text_raw) "
        "VALUES ('aff_1', 'src_a', 'src_a:r1:1', NULL, 'chard + anchovy')"
    )
    row = conn.execute("SELECT subject_entity_id FROM affinity_groups WHERE affinity_id = 'aff_1'").fetchone()
    assert row["subject_entity_id"] is None


def test_pairing_observations_paired_entity_id_nullable(conn):
    _seed_source_and_raw_row(conn)
    conn.execute("INSERT INTO entities (entity_id) VALUES ('ent_1')")
    conn.execute(
        "INSERT INTO pairing_observations "
        "(observation_id, source_id, source_record_id, subject_entity_id, paired_entity_id) "
        "VALUES ('obs_1', 'src_a', 'src_a:r1:1', 'ent_1', NULL)"
    )
    row = conn.execute(
        "SELECT paired_entity_id FROM pairing_observations WHERE observation_id = 'obs_1'"
    ).fetchone()
    assert row["paired_entity_id"] is None


def test_entity_source_names_entity_id_nullable(conn):
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    conn.execute(
        "INSERT INTO entity_source_names "
        "(source_name_id, source_id, source_text, source_role, entity_id) "
        "VALUES ('sn_1', 'src_a', 'CARAMEL', 'pairing_entry', NULL)"
    )
    row = conn.execute("SELECT entity_id FROM entity_source_names WHERE source_name_id = 'sn_1'").fetchone()
    assert row["entity_id"] is None


def test_affinity_members_member_entity_id_nullable(conn):
    _seed_source_and_raw_row(conn)
    conn.execute(
        "INSERT INTO affinity_groups (affinity_id, source_id, source_record_id, affinity_text_raw) "
        "VALUES ('aff_1', 'src_a', 'src_a:r1:1', 'chard + garlic')"
    )
    conn.execute(
        "INSERT INTO affinity_members (affinity_id, member_order, member_entity_id, member_text_raw) "
        "VALUES ('aff_1', 1, NULL, 'garlic')"
    )
    row = conn.execute(
        "SELECT member_entity_id FROM affinity_members WHERE affinity_id = 'aff_1' AND member_order = 1"
    ).fetchone()
    assert row["member_entity_id"] is None


def test_entities_parent_entity_id_nullable(conn):
    conn.execute("INSERT INTO entities (entity_id, parent_entity_id) VALUES ('ent_1', NULL)")
    row = conn.execute("SELECT parent_entity_id FROM entities WHERE entity_id = 'ent_1'").fetchone()
    assert row["parent_entity_id"] is None


# ---------------------------------------------------------------------------
# Unique / natural-key constraints
# ---------------------------------------------------------------------------

def test_entity_source_names_unique_natural_key(conn):
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES ('src_a', 'A', 'fmt_a', 'project_owned_demo')"
    )
    conn.execute(
        "INSERT INTO entity_source_names (source_name_id, source_id, source_text, source_role) "
        "VALUES ('sn_1', 'src_a', 'APPLE', 'subject')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        # Different surrogate id, same (source_id, source_text, source_role).
        conn.execute(
            "INSERT INTO entity_source_names (source_name_id, source_id, source_text, source_role) "
            "VALUES ('sn_2', 'src_a', 'APPLE', 'subject')"
        )


def test_pairing_observations_unique_source_record(conn):
    _seed_source_and_raw_row(conn)
    conn.execute("INSERT INTO entities (entity_id) VALUES ('ent_1')")
    conn.execute(
        "INSERT INTO pairing_observations (observation_id, source_id, source_record_id, subject_entity_id) "
        "VALUES ('obs_1', 'src_a', 'src_a:r1:1', 'ent_1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pairing_observations (observation_id, source_id, source_record_id, subject_entity_id) "
            "VALUES ('obs_2', 'src_a', 'src_a:r1:1', 'ent_1')"
        )


def test_affinity_groups_unique_source_record(conn):
    _seed_source_and_raw_row(conn)
    conn.execute(
        "INSERT INTO affinity_groups (affinity_id, source_id, source_record_id) "
        "VALUES ('aff_1', 'src_a', 'src_a:r1:1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO affinity_groups (affinity_id, source_id, source_record_id) "
            "VALUES ('aff_2', 'src_a', 'src_a:r1:1')"
        )


def test_primary_key_rejects_duplicate(conn):
    conn.execute("INSERT INTO entities (entity_id) VALUES ('ent_1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entities (entity_id) VALUES ('ent_1')")


def test_import_mappings_key_is_source_format_target_file_target_field(conn):
    conn.execute(
        "INSERT INTO import_mappings (source_format, target_file, target_field) "
        "VALUES ('fmt_a', 'raw_source_rows.csv', 'subject_raw')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO import_mappings (source_format, target_file, target_field) "
            "VALUES ('fmt_a', 'raw_source_rows.csv', 'subject_raw')"
        )


def test_affinity_members_composite_primary_key(conn):
    _seed_source_and_raw_row(conn)
    conn.execute(
        "INSERT INTO affinity_groups (affinity_id, source_id, source_record_id) "
        "VALUES ('aff_1', 'src_a', 'src_a:r1:1')"
    )
    conn.execute("INSERT INTO affinity_members (affinity_id, member_order) VALUES ('aff_1', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO affinity_members (affinity_id, member_order) VALUES ('aff_1', 1)")


def test_strength_mappings_key_is_format_and_marker(conn):
    conn.execute(
        "INSERT INTO strength_mappings (input_source_format, marker_key) VALUES ('fmt_a', 'plain')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO strength_mappings (input_source_format, marker_key) VALUES ('fmt_a', 'plain')"
        )


def test_attribute_labels_key_is_format_and_label(conn):
    conn.execute(
        "INSERT INTO attribute_labels (source_format, source_label) VALUES ('fmt_a', 'Season')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO attribute_labels (source_format, source_label) VALUES ('fmt_a', 'Season')"
        )


def test_affinity_split_rules_key_is_source_format(conn):
    conn.execute("INSERT INTO affinity_split_rules (source_format) VALUES ('fmt_a')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO affinity_split_rules (source_format) VALUES ('fmt_a')")


# ---------------------------------------------------------------------------
# Portability / layering rules
# ---------------------------------------------------------------------------

def _sql_without_comments(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("--"))


def test_schema_sql_has_no_autoincrement_or_rowid_tricks():
    ddl_only = _sql_without_comments(db.SCHEMA_SQL_PATH)
    assert "AUTOINCREMENT" not in ddl_only.upper()
    assert "WITHOUT ROWID" not in ddl_only.upper()


def test_schema_sql_has_no_pragma():
    ddl_only = _sql_without_comments(db.SCHEMA_SQL_PATH)
    assert "PRAGMA" not in ddl_only.upper()


def test_csv_io_module_never_uses_pragma():
    from flavor_pairing.store import csv_io

    source_text = Path(csv_io.__file__).read_text(encoding="utf-8")
    assert "PRAGMA" not in source_text.upper()
