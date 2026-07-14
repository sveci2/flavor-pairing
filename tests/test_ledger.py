"""CP3A tests: durable ledger persistence, latest-completed-run selection,
and reload after the disposable SQLite working database is deleted
(docs/DATA_FOUNDATION_PLAN.md §3, §16 test_ledger.py).

Ledger reload here is intentionally limited to import_runs/run_rows plus
test-controlled raw_source_rows reconstruction via store.csv_io — a full
raw-ingestion ledger (reconstructing raw_source_rows from its own ledger) is
out of scope for CP3A.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flavor_pairing.ingest.identity import RawRowContent
from flavor_pairing.ingest.runs import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_IN_PROGRESS,
    RUN_STATUSES,
    current_version,
    record_completed_run,
    record_failed_run,
    record_in_progress_run,
)
from flavor_pairing.store import csv_io, db, ledger

SOURCE_ID = "src_test_ledger"


def make_clock(start=None, step=timedelta(seconds=1)):
    state = {"t": start or datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def _clock():
        current = state["t"]
        state["t"] = current + step
        return current

    return _clock


def row(subject, entry):
    return RawRowContent(subject_raw=subject, entry_raw=entry)


def _seed_source(connection, source_id=SOURCE_ID):
    connection.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES (?, 'Test ledger source', 'fmt_test', 'project_owned_demo')",
        (source_id,),
    )
    connection.commit()


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    _seed_source(connection)
    yield connection
    connection.close()


def test_run_status_constant_matches_approved_vocabulary():
    assert RUN_STATUSES == {RUN_STATUS_COMPLETED, RUN_STATUS_FAILED, RUN_STATUS_IN_PROGRESS}


def test_latest_completed_run_defines_current_version(conn, tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    record_completed_run(conn, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root)
    record_completed_run(
        conn, SOURCE_ID, [row("A", "1"), row("B", "2")], clock=clock, ledger_root=ledger_root
    )
    outcome3 = record_completed_run(
        conn,
        SOURCE_ID,
        [row("A", "1"), row("B", "2"), row("C", "3")],
        clock=clock,
        ledger_root=ledger_root,
    )

    current = current_version(conn, SOURCE_ID)
    assert current.run_id == outcome3.run_id
    assert len(current.members) == 3


def test_failed_run_ignored_for_current_version_even_if_more_recent(conn, tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    completed = record_completed_run(
        conn, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root
    )
    record_failed_run(conn, SOURCE_ID, clock=clock, ledger_root=ledger_root)  # later, but failed

    current = current_version(conn, SOURCE_ID)
    assert current.run_id == completed.run_id


def test_in_progress_run_ignored_for_current_version(conn, tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    completed = record_completed_run(
        conn, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root
    )
    record_in_progress_run(conn, SOURCE_ID, clock=clock)  # never resolves

    current = current_version(conn, SOURCE_ID)
    assert current.run_id == completed.run_id


def test_failed_run_writes_no_run_rows(conn, tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    record_completed_run(conn, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root)
    failed = record_failed_run(conn, SOURCE_ID, clock=clock, ledger_root=ledger_root)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM run_rows WHERE run_id = ?", (failed.run_id,)
    ).fetchone()["n"]
    assert count == 0


def test_in_progress_run_not_mirrored_to_ledger(conn, tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    record_in_progress_run(conn, SOURCE_ID, clock=clock)
    assert ledger.read_import_runs(SOURCE_ID, ledger_root=ledger_root) == []


def test_ledger_append_only_never_rewrites_prior_rows(tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    connection = db.open_database(":memory:")
    _seed_source(connection)

    record_completed_run(connection, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root)
    first_snapshot = ledger.read_import_runs(SOURCE_ID, ledger_root=ledger_root)

    record_completed_run(
        connection, SOURCE_ID, [row("A", "1"), row("B", "2")], clock=clock, ledger_root=ledger_root
    )
    second_snapshot = ledger.read_import_runs(SOURCE_ID, ledger_root=ledger_root)

    assert second_snapshot[: len(first_snapshot)] == first_snapshot
    assert len(second_snapshot) == len(first_snapshot) + 1
    connection.close()


def test_ledger_csv_has_utf8_bom_and_lf_newlines(tmp_path):
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    connection = db.open_database(":memory:")
    _seed_source(connection)
    record_completed_run(connection, SOURCE_ID, [row("A", "1")], clock=clock, ledger_root=ledger_root)
    connection.close()

    import_runs_path, run_rows_path = ledger.ledger_paths(SOURCE_ID, ledger_root)
    for path in (import_runs_path, run_rows_path):
        raw_bytes = path.read_bytes()
        assert raw_bytes.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in raw_bytes


def test_ledger_survives_sqlite_deletion(tmp_path):
    """Delete the disposable SQLite working DB; rebuild it from raw export +
    ledger CSVs alone and confirm the run history is reproduced identically.
    """
    clock = make_clock()
    ledger_root = tmp_path / "ledger"
    db_path = tmp_path / "working.sqlite"

    connection = db.open_database(db_path)
    _seed_source(connection)
    rows = [row("A", "1"), row("B", "2")]
    outcome = record_completed_run(connection, SOURCE_ID, rows, clock=clock, ledger_root=ledger_root)

    raw_export_dir = tmp_path / "raw_export"
    csv_io.export_table(
        connection, csv_io.TABLES["raw_source_rows"], raw_export_dir / "raw_source_rows.csv"
    )
    connection.close()
    db_path.unlink()
    assert not db_path.exists()

    rebuilt = db.open_database(db_path)
    _seed_source(rebuilt)
    csv_io.import_table(
        rebuilt, csv_io.TABLES["raw_source_rows"], raw_export_dir / "raw_source_rows.csv"
    )
    counts = ledger.load_ledger_into_db(rebuilt, SOURCE_ID, ledger_root=ledger_root)

    assert counts == {"import_runs": 1, "run_rows": 2}
    current = current_version(rebuilt, SOURCE_ID)
    assert current.run_id == outcome.run_id
    assert len(current.members) == 2
    rebuilt.close()
