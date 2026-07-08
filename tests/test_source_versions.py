"""CP3A tests: source-record identity and run membership across source
versions (docs/DECISIONS.md §H; docs/DATA_FOUNDATION_PLAN.md §3, §16).

Exercises flavor_pairing.ingest.identity/runs directly against ordered lists
of RawRowContent, standing in for successive versions of one source file.
Full external-file reading is out of scope for CP3A (see AGENTS.md/CLAUDE.md
non-negotiable rules and the approved CP3A scope) — these tests never touch
data/imports_private/ or any real file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flavor_pairing.ingest.identity import RawRowContent, assign_source_record_ids
from flavor_pairing.ingest.runs import current_version, record_completed_run
from flavor_pairing.store import db

SOURCE_ID = "src_test_versions"


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    connection.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES (?, 'Test versions source', 'fmt_test', 'project_owned_demo')",
        (SOURCE_ID,),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def ledger_root(tmp_path):
    # Every record_completed_run call below must pass this explicitly — the
    # default ledger root is the real committed data/ledger/, which tests
    # must never write to.
    return tmp_path / "ledger"


def make_clock(start=None, step=timedelta(seconds=1)):
    """A deterministic, monotonically increasing UTC clock (never real time)."""
    state = {"t": start or datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def _clock():
        current = state["t"]
        state["t"] = current + step
        return current

    return _clock


def row(subject, entry, quality=None, payload=None):
    return RawRowContent(
        subject_raw=subject, entry_raw=entry, quality_raw=quality, raw_payload_json=payload
    )


def _raw_rows(connection, source_id=SOURCE_ID):
    return connection.execute(
        "SELECT source_record_id, source_order, subject_raw, entry_raw FROM raw_source_rows "
        "WHERE source_id = ? ORDER BY source_order",
        (source_id,),
    ).fetchall()


def _run_rows(connection, run_id):
    return connection.execute(
        "SELECT source_record_id, source_order FROM run_rows WHERE run_id = ? "
        "ORDER BY source_order",
        (run_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Identity: pure function, no database involved
# ---------------------------------------------------------------------------

def test_identity_is_content_derived_not_positional():
    forward = ["A", "B", "C"]
    reordered = ["C", "A", "B"]
    forward_ids = assign_source_record_ids(SOURCE_ID, [row(s, "x") for s in forward])
    reordered_ids = assign_source_record_ids(SOURCE_ID, [row(s, "x") for s in reordered])
    assert dict(zip(forward, forward_ids)) == dict(zip(reordered, reordered_ids))


def test_identity_changes_when_content_changes():
    id_before = assign_source_record_ids(SOURCE_ID, [row("A", "old")])[0]
    id_after = assign_source_record_ids(SOURCE_ID, [row("A", "new")])[0]
    assert id_before != id_after


def test_repeated_identical_rows_get_distinct_occurrence_indexes():
    ids = assign_source_record_ids(SOURCE_ID, [row("A", "x"), row("A", "x"), row("B", "y")])
    assert ids[0] != ids[1]
    assert ids[0].rsplit(":", 1)[0] == ids[1].rsplit(":", 1)[0]  # same content hash
    assert ids[0].endswith(":1")
    assert ids[1].endswith(":2")
    assert ids[2].endswith(":1")  # distinct content starts its own occurrence count


# ---------------------------------------------------------------------------
# record_completed_run: insert / remove / reorder / edit / rerun scenarios
# ---------------------------------------------------------------------------

def test_identical_rerun_inserts_no_new_raw_rows(conn, ledger_root):
    clock = make_clock()
    rows = [row("APPLE", "cinnamon"), row("APPLE", "walnut")]

    outcome1 = record_completed_run(conn, SOURCE_ID, rows, clock=clock, ledger_root=ledger_root)
    outcome2 = record_completed_run(conn, SOURCE_ID, rows, clock=clock, ledger_root=ledger_root)

    assert outcome1.run_id != outcome2.run_id
    assert len(_raw_rows(conn)) == 2
    assert outcome2.inserted_source_record_ids == ()
    assert len(_run_rows(conn, outcome1.run_id)) == 2
    assert len(_run_rows(conn, outcome2.run_id)) == 2  # new run still has full membership


def test_inserted_row_does_not_shift_existing_identities(conn, ledger_root):
    clock = make_clock()
    v1 = [row("APPLE", "cinnamon"), row("APPLE", "walnut")]
    record_completed_run(conn, SOURCE_ID, v1, clock=clock, ledger_root=ledger_root)
    ids_before = {r["source_record_id"] for r in _raw_rows(conn)}

    v2 = [row("APPLE", "cinnamon"), row("APPLE", "clove"), row("APPLE", "walnut")]
    outcome2 = record_completed_run(conn, SOURCE_ID, v2, clock=clock, ledger_root=ledger_root)

    assert len(outcome2.inserted_source_record_ids) == 1
    ids_after = {r["source_record_id"] for r in _raw_rows(conn)}
    assert ids_before <= ids_after  # nothing about the original two rows changed
    assert len(ids_after) == 3


def test_removed_row_excluded_from_current_but_preserved_in_raw(conn, ledger_root):
    clock = make_clock()
    v1 = [row("APPLE", "cinnamon"), row("APPLE", "walnut"), row("APPLE", "clove")]
    record_completed_run(conn, SOURCE_ID, v1, clock=clock, ledger_root=ledger_root)

    v2 = [row("APPLE", "cinnamon"), row("APPLE", "walnut")]  # 'clove' removed
    outcome2 = record_completed_run(conn, SOURCE_ID, v2, clock=clock, ledger_root=ledger_root)

    assert len(_raw_rows(conn)) == 3  # nothing deleted from raw
    current = current_version(conn, SOURCE_ID)
    assert current.run_id == outcome2.run_id
    assert len(current.members) == 2
    removed_id = next(r["source_record_id"] for r in _raw_rows(conn) if r["entry_raw"] == "clove")
    assert removed_id not in {member[0] for member in current.members}


def test_reordered_rows_keep_identity(conn, ledger_root):
    clock = make_clock()
    v1 = [row("APPLE", "cinnamon"), row("APPLE", "walnut")]
    record_completed_run(conn, SOURCE_ID, v1, clock=clock, ledger_root=ledger_root)
    ids_before = {r["source_record_id"] for r in _raw_rows(conn)}

    v2 = [row("APPLE", "walnut"), row("APPLE", "cinnamon")]  # reordered, same content
    outcome2 = record_completed_run(conn, SOURCE_ID, v2, clock=clock, ledger_root=ledger_root)

    assert outcome2.inserted_source_record_ids == ()  # no new raw rows at all
    assert {r["source_record_id"] for r in _raw_rows(conn)} == ids_before

    run2_members = _run_rows(conn, outcome2.run_id)
    assert [m["source_order"] for m in run2_members] == [1, 2]  # new version's order

    # raw_source_rows.source_order still reflects order at first ingestion.
    raw_orders = {f"{r['subject_raw']}|{r['entry_raw']}": r["source_order"] for r in _raw_rows(conn)}
    assert raw_orders["APPLE|cinnamon"] == 1
    assert raw_orders["APPLE|walnut"] == 2


def test_edited_row_becomes_new_record_old_preserved_but_excluded(conn, ledger_root):
    clock = make_clock()
    record_completed_run(
        conn, SOURCE_ID, [row("APPLE", "cinnamon")], clock=clock, ledger_root=ledger_root
    )

    outcome2 = record_completed_run(
        conn, SOURCE_ID, [row("APPLE", "cinnamon-toast")], clock=clock, ledger_root=ledger_root
    )

    assert len(_raw_rows(conn)) == 2  # old preserved, new inserted
    assert len(outcome2.inserted_source_record_ids) == 1
    current = current_version(conn, SOURCE_ID)
    assert len(current.members) == 1
    assert current.members[0][0] == outcome2.inserted_source_record_ids[0]


def test_occurrence_index_recomputed_safely_across_versions(conn, ledger_root):
    clock = make_clock()
    v1 = [row("APPLE", "x"), row("APPLE", "x")]  # two identical rows
    record_completed_run(conn, SOURCE_ID, v1, clock=clock, ledger_root=ledger_root)
    assert len(_raw_rows(conn)) == 2

    v2 = [row("APPLE", "x")]  # only one now (occurrence 1 already known)
    outcome2 = record_completed_run(conn, SOURCE_ID, v2, clock=clock, ledger_root=ledger_root)
    assert outcome2.inserted_source_record_ids == ()
    assert len(_raw_rows(conn)) == 2  # nothing new, nothing removed from raw
    assert len(current_version(conn, SOURCE_ID).members) == 1

    v3 = [row("APPLE", "x"), row("APPLE", "x"), row("APPLE", "x")]  # three now
    outcome3 = record_completed_run(conn, SOURCE_ID, v3, clock=clock, ledger_root=ledger_root)
    assert len(outcome3.inserted_source_record_ids) == 1  # only occurrence 3 is new
    assert len(_raw_rows(conn)) == 3
    assert len(current_version(conn, SOURCE_ID).members) == 3
