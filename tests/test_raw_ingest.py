"""CP3B tests: mapping-driven raw ingestion of external CSVs
(docs/DATA_FOUNDATION_PLAN.md §4, §10, §16 test_raw_ingest.py).

Uses conftest.py's build_config fixture (fmt_alpha: a 2-column mapping with
no quality_raw column) plus a locally overridden 3-column fmt_beta format,
so no shared fixture needs editing. All CSV fixtures written here are
external "source files" distinct from the config CSVs build_config writes.
Never touches data/imports_private/ — sources here are project_owned_demo,
routed to a tmp_path-based public ledger root only.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG
from flavor_pairing.config.loaders import load_config
from flavor_pairing.ingest.raw_ingest import IngestError, ingest_file, read_mapped_csv
from flavor_pairing.ingest.runs import current_version
from flavor_pairing.store import db

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "flavor_pairing"


def _write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    return path


def _seed_sources(connection, rows):
    for row in rows:
        connection.execute(
            "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
            "VALUES (?, ?, ?, ?)",
            (row["source_id"], row["source_name"], row["source_format"], row["rights_status"]),
        )
    connection.commit()


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def two_column_config(build_config, conn):
    """fmt_alpha: subject/entry only, matching conftest's DEFAULT_CONFIG."""
    config_dir = build_config()
    config = load_config(config_dir)
    _seed_sources(conn, DEFAULT_CONFIG["sources.csv"])
    return config


@pytest.fixture
def three_column_config(build_config, conn):
    """fmt_beta: subject/entry/quality, added alongside fmt_alpha."""
    sources = [dict(DEFAULT_CONFIG["sources.csv"][0])]
    sources.append(
        {
            "source_id": "src_beta",
            "source_name": "Beta demo source",
            "source_format": "fmt_beta",
            "rights_status": "project_owned_demo",
            "allowed_use": "software_testing",
        }
    )
    mappings = [dict(row) for row in DEFAULT_CONFIG["import_mappings.csv"]]
    mappings.extend(
        [
            {
                "source_format": "fmt_beta",
                "input_column": "col_subject",
                "target_file": "raw_source_rows.csv",
                "target_field": "subject_raw",
                "transform_rule": "copy exactly",
                "required": "1",
            },
            {
                "source_format": "fmt_beta",
                "input_column": "col_entry",
                "target_file": "raw_source_rows.csv",
                "target_field": "entry_raw",
                "transform_rule": "copy exactly",
                "required": "1",
            },
            {
                "source_format": "fmt_beta",
                "input_column": "col_quality",
                "target_file": "raw_source_rows.csv",
                "target_field": "quality_raw",
                "transform_rule": "copy exactly",
                "required": "0",
            },
        ]
    )
    config_dir = build_config(overrides={"sources.csv": sources, "import_mappings.csv": mappings})
    config = load_config(config_dir)
    _seed_sources(conn, sources)
    return config


def _raw_rows(connection, source_id):
    return connection.execute(
        "SELECT source_record_id, source_order, subject_raw, entry_raw, quality_raw, "
        "raw_payload_json FROM raw_source_rows WHERE source_id = ? ORDER BY source_order",
        (source_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Mapping-driven ingestion
# ---------------------------------------------------------------------------

def test_two_column_mapping_ingests_subject_and_entry(two_column_config, conn, tmp_path):
    csv_path = _write_csv(
        tmp_path / "v1.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"], ["APPLE", "walnut"]]
    )
    outcome = ingest_file(
        two_column_config, "src_alpha", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
    )
    rows = _raw_rows(conn, "src_alpha")
    assert outcome.row_count == 2
    assert len(rows) == 2
    assert all(row["quality_raw"] is None for row in rows)
    assert {row["subject_raw"] for row in rows} == {"APPLE"}
    assert {row["entry_raw"] for row in rows} == {"cinnamon", "walnut"}


def test_three_column_mapping_ingests_quality_raw(three_column_config, conn, tmp_path):
    csv_path = _write_csv(
        tmp_path / "v1.csv",
        ["col_subject", "col_entry", "col_quality"],
        [["apple", "cinnamon", "heaven"], ["apple", "walnut", ""]],
    )
    ingest_file(
        three_column_config, "src_beta", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
    )
    rows = {row["entry_raw"]: row["quality_raw"] for row in _raw_rows(conn, "src_beta")}
    assert rows["cinnamon"] == "heaven"
    assert rows["walnut"] is None  # blank cell -> NULL (project null convention)


# ---------------------------------------------------------------------------
# Missing / malformed columns
# ---------------------------------------------------------------------------

def test_missing_required_column_raises_and_records_failed_run(two_column_config, conn, tmp_path):
    csv_path = _write_csv(tmp_path / "v1.csv", ["col_entry"], [["cinnamon"]])  # col_subject missing

    with pytest.raises(IngestError, match=r"col_subject"):
        ingest_file(
            two_column_config, "src_alpha", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
        )

    assert _raw_rows(conn, "src_alpha") == []
    run_row = conn.execute(
        "SELECT status, row_count FROM import_runs WHERE source_id = 'src_alpha'"
    ).fetchone()
    assert run_row["status"] == "failed"
    assert run_row["row_count"] is None
    assert conn.execute("SELECT COUNT(*) AS n FROM run_rows").fetchone()["n"] == 0


def test_ragged_row_with_extra_columns_raises_ingest_error(two_column_config, conn, tmp_path):
    path = tmp_path / "v1.csv"
    # Hand-write a ragged line the csv module's writer wouldn't normally produce.
    path.write_text(
        "﻿col_subject,col_entry\r\nAPPLE,cinnamon,extra_unmapped_value\r\n",
        encoding="utf-8",
    )
    with pytest.raises(IngestError, match=r"ragged"):
        ingest_file(
            two_column_config, "src_alpha", path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
        )
    assert _raw_rows(conn, "src_alpha") == []


# ---------------------------------------------------------------------------
# raw_payload_json / exact value preservation
# ---------------------------------------------------------------------------

def test_raw_payload_json_preserves_full_original_row(two_column_config, conn, tmp_path):
    csv_path = _write_csv(
        tmp_path / "v1.csv",
        ["col_subject", "col_entry", "col_extra"],
        [["APPLE", "cinnamon", "unmapped value"]],
    )
    ingest_file(
        two_column_config, "src_alpha", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
    )
    row = _raw_rows(conn, "src_alpha")[0]
    payload = json.loads(row["raw_payload_json"])
    assert payload == {"col_subject": "APPLE", "col_entry": "cinnamon", "col_extra": "unmapped value"}


def test_exact_raw_value_preservation_no_trim_or_case_fold(two_column_config, conn, tmp_path):
    csv_path = _write_csv(
        tmp_path / "v1.csv", ["col_subject", "col_entry"], [["  Apple  ", "CINnamon"]]
    )
    ingest_file(
        two_column_config, "src_alpha", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
    )
    row = _raw_rows(conn, "src_alpha")[0]
    assert row["subject_raw"] == "  Apple  "
    assert row["entry_raw"] == "CINnamon"


# ---------------------------------------------------------------------------
# Scale / generality (complements the existing runtime-wide
# test_runtime_has_no_hardcoded_sample_source_ids meta-test, which already
# re-scans these new files automatically)
# ---------------------------------------------------------------------------

def test_ingestion_works_for_arbitrary_source_id_and_row_count(build_config, conn, tmp_path):
    sources = [
        {
            "source_id": "src_generated_9f2c",
            "source_name": "Generated source",
            "source_format": "fmt_alpha",
            "rights_status": "project_owned_demo",
            "allowed_use": "software_testing",
        }
    ]
    config_dir = build_config(overrides={"sources.csv": sources})
    config = load_config(config_dir)
    _seed_sources(conn, sources)

    row_count = 37
    body = [[f"SUBJECT_{i}", f"entry_{i}"] for i in range(row_count)]
    csv_path = _write_csv(tmp_path / "generated.csv", ["col_subject", "col_entry"], body)

    outcome = ingest_file(
        config, "src_generated_9f2c", csv_path, conn, private_ledger_root=tmp_path / "private", public_ledger_root=tmp_path / "public"
    )
    assert outcome.row_count == row_count
    assert len(_raw_rows(conn, "src_generated_9f2c")) == row_count


# ---------------------------------------------------------------------------
# Byte-identical rerun / changed file
# ---------------------------------------------------------------------------

def test_byte_identical_rerun_inserts_no_new_rows(two_column_config, conn, tmp_path):
    csv_path = _write_csv(
        tmp_path / "v1.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"], ["APPLE", "walnut"]]
    )
    ledger_root = tmp_path / "public_ledger"
    outcome1 = ingest_file(
        two_column_config,
        "src_alpha",
        csv_path,
        conn,
        private_ledger_root=tmp_path / "private",
        public_ledger_root=ledger_root,
    )
    outcome2 = ingest_file(
        two_column_config,
        "src_alpha",
        csv_path,
        conn,
        private_ledger_root=tmp_path / "private",
        public_ledger_root=ledger_root,
    )

    assert outcome1.run_id != outcome2.run_id
    assert outcome1.input_file_hash == outcome2.input_file_hash
    assert outcome2.inserted_source_record_ids == ()
    assert len(_raw_rows(conn, "src_alpha")) == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM import_runs WHERE source_id = 'src_alpha'"
    ).fetchone()["n"] == 2


def test_changed_file_creates_new_run_and_correct_current_version(two_column_config, conn, tmp_path):
    ledger_root = tmp_path / "public_ledger"
    v1 = _write_csv(tmp_path / "v1.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"]])
    ingest_file(
        two_column_config,
        "src_alpha",
        v1,
        conn,
        private_ledger_root=tmp_path / "private",
        public_ledger_root=ledger_root,
    )

    v2 = _write_csv(
        tmp_path / "v2.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"], ["APPLE", "clove"]]
    )
    outcome2 = ingest_file(
        two_column_config,
        "src_alpha",
        v2,
        conn,
        private_ledger_root=tmp_path / "private",
        public_ledger_root=ledger_root,
    )

    assert outcome2.input_file_hash != None  # noqa: E711 - explicit, not a bool check
    current = current_version(conn, "src_alpha")
    assert current.run_id == outcome2.run_id
    assert len(current.members) == 2
    assert len(_raw_rows(conn, "src_alpha")) == 2  # nothing removed from raw either


# ---------------------------------------------------------------------------
# No raw UPDATE/DELETE through application code (static scan)
# ---------------------------------------------------------------------------

def test_no_update_or_delete_against_raw_source_rows_in_ingestion_code():
    scanned = list((RUNTIME_DIR / "ingest").glob("*.py")) + [
        Path(__file__).resolve().parents[1] / "scripts" / "import_to_raw.py"
    ]
    assert scanned, "expected at least the ingest package plus the CLI script"
    for path in scanned:
        text_upper = path.read_text(encoding="utf-8").upper()
        assert "UPDATE RAW_SOURCE_ROWS" not in text_upper, f"{path} updates raw_source_rows"
        assert "DELETE FROM RAW_SOURCE_ROWS" not in text_upper, f"{path} deletes from raw_source_rows"
