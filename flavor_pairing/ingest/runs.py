"""Import-run orchestration: record identity, run membership, and current
version resolution (docs/SCHEMA.md §11; docs/DECISIONS.md §H;
docs/DATA_FOUNDATION_PLAN.md §3).

Three run outcomes are modeled as three distinct entry points rather than one
function with a "simulate failure" flag, so that failure is structural (a
failed run simply cannot be given rows to insert) rather than an
after-the-fact override:

- :func:`record_completed_run` — the only entry point that may write to
  ``raw_source_rows`` and ``run_rows``. All-or-nothing: on any error, the
  SQLite transaction is rolled back and nothing is appended to the ledger.
- :func:`record_failed_run` — writes only an ``import_runs`` row with
  ``status='failed'``; never touches ``raw_source_rows`` or ``run_rows``.
- :func:`record_in_progress_run` — writes only an ``import_runs`` row with
  ``status='in_progress'``. Deliberately *not* mirrored to the durable
  ledger: an in-progress run is transient operational state, not yet a
  durable historical fact. If it never resolves (e.g. a crash), nothing is
  lost by its absence from the ledger; if it resolves, the resolving call
  (completed/failed) is what gets recorded.

This module contains no file reading, column mapping, parsing, or
normalization logic — see ``docs/DATA_FOUNDATION_PLAN.md`` §20-21 for what
remains out of scope for CP3A.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from flavor_pairing.ingest.identity import RawRowContent, assign_source_record_ids
from flavor_pairing.store import ledger

__all__ = [
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_IN_PROGRESS",
    "RUN_STATUSES",
    "CurrentVersion",
    "RunOutcome",
    "compute_input_file_hash",
    "current_version",
    "make_run_id",
    "record_completed_run",
    "record_failed_run",
    "record_in_progress_run",
]

# Module-level status vocabulary. Kept in sync by hand with the CHECK
# constraint on import_runs.status in store/schema.sql — if this set ever
# changes, that constraint must change with it.
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_IN_PROGRESS = "in_progress"
RUN_STATUSES = frozenset({RUN_STATUS_COMPLETED, RUN_STATUS_FAILED, RUN_STATUS_IN_PROGRESS})

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class RunOutcome:
    """What one call to a ``record_*_run`` function did."""

    run_id: str
    source_id: str
    status: str
    started_at: str
    finished_at: Optional[str]
    input_file_hash: Optional[str]
    row_count: Optional[int]
    inserted_source_record_ids: Tuple[str, ...] = field(default_factory=tuple)
    run_row_source_record_ids: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CurrentVersion:
    """The row membership of a source's latest completed run."""

    run_id: str
    source_id: str
    members: Tuple[Tuple[str, int], ...]  # (source_record_id, source_order), in source_order


def _utc_now(clock: Optional[Clock] = None) -> datetime:
    now = clock() if clock is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("clock() must return a timezone-aware (UTC) datetime")
    return now.astimezone(timezone.utc)


def make_run_id(source_id: str, *, clock: Optional[Clock] = None) -> str:
    """``<UTC timestamp>Z_<source_id>``, sortable and never dependent on local time.

    e.g. ``20260706T120000123456Z_src_github_flavor_bible``
    (docs/DATA_FOUNDATION_PLAN.md §3). Callers needing deterministic,
    non-flaky test output should pass an explicit ``clock`` (or bypass this
    entirely via the ``run_id`` override on the ``record_*_run`` functions).
    """
    now = _utc_now(clock)
    return f"{now.strftime('%Y%m%dT%H%M%S%f')}Z_{source_id}"


def _iso(clock: Optional[Clock] = None) -> str:
    return _utc_now(clock).isoformat()


def compute_input_file_hash(rows: Sequence[RawRowContent]) -> str:
    """Full SHA-256 hex digest identifying one version's content.

    CP3A stand-in: CP3A has no real external-file reader yet, so this hashes
    the canonical ordered row content passed to ``record_completed_run``
    rather than actual input-file bytes. Once real file ingestion exists
    (raw_ingest.py, out of scope here), that layer should replace or
    supplement this with a hash of the actual file bytes so that
    byte-for-byte-identical files always match regardless of how they were
    parsed into rows.
    """
    canonical = json.dumps(
        [[r.subject_raw, r.entry_raw, r.quality_raw, r.raw_payload_json] for r in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_version(connection: sqlite3.Connection, source_id: str) -> Optional[CurrentVersion]:
    """The row membership of the latest *completed* run for ``source_id``.

    Runs with ``status`` other than ``completed`` (failed or in_progress)
    are never considered, regardless of how recent their timestamps are
    (docs/DECISIONS.md §H). Returns ``None`` if the source has no completed
    run. Ties on ``finished_at`` are broken by ``run_id``, which sorts
    chronologically since it is a UTC-timestamp prefix.
    """
    run_row = connection.execute(
        "SELECT run_id FROM import_runs WHERE source_id = ? AND status = ? "
        "ORDER BY finished_at DESC, run_id DESC LIMIT 1",
        (source_id, RUN_STATUS_COMPLETED),
    ).fetchone()
    if run_row is None:
        return None
    run_id = run_row["run_id"]
    member_rows = connection.execute(
        "SELECT source_record_id, source_order FROM run_rows "
        "WHERE run_id = ? ORDER BY source_order",
        (run_id,),
    ).fetchall()
    members = tuple((row["source_record_id"], row["source_order"]) for row in member_rows)
    return CurrentVersion(run_id=run_id, source_id=source_id, members=members)


def record_completed_run(
    connection: sqlite3.Connection,
    source_id: str,
    rows: Sequence[RawRowContent],
    *,
    run_id: Optional[str] = None,
    clock: Optional[Clock] = None,
    ledger_root: Path = ledger.DEFAULT_LEDGER_ROOT,
    input_file_hash: Optional[str] = None,
) -> RunOutcome:
    """Ingest one version's rows as a single all-or-nothing completed run.

    - Computes each row's content-derived ``source_record_id`` (identity.py).
    - Inserts into ``raw_source_rows`` only rows whose ``source_record_id``
      is not already present for this source (append-only; never UPDATE or
      DELETE). A newly inserted row's ``source_order`` is its 1-based
      position within *this* version — "order at first ingestion"
      (docs/SCHEMA.md §2) — and is never revisited by a later run.
    - Writes complete ``run_rows`` membership for every row in this version
      (new or previously known), ordered by this version's row order.
    - Writes one ``import_runs`` row with ``status='completed'``.
    - On any error, rolls back the SQLite transaction so no partial raw rows
      or run_rows are left behind, then re-raises.
    - Mirrors the completed ``import_runs``/``run_rows`` rows into the
      durable append-only ledger only after the SQLite commit succeeds.

    ``input_file_hash``: override for the CP3A row-content stand-in
    (:func:`compute_input_file_hash`). CP3B's ``raw_ingest.ingest_file``
    passes the real external file's byte-for-byte SHA-256 here, so that
    "was this a repeat of the same file" is a direct comparison of stored
    ``import_runs.input_file_hash`` values rather than a hash of already-
    parsed rows. Callers that have no real file (as in every CP3A test)
    omit this and get the CP3A stand-in unchanged.
    """
    resolved_run_id = run_id or make_run_id(source_id, clock=clock)
    started_at = _iso(clock)
    record_ids = assign_source_record_ids(source_id, rows)
    resolved_input_file_hash = (
        input_file_hash if input_file_hash is not None else compute_input_file_hash(rows)
    )
    row_count = len(rows)

    try:
        existing_ids = {
            row["source_record_id"]
            for row in connection.execute(
                "SELECT source_record_id FROM raw_source_rows WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        }

        inserted: List[str] = []
        for position, (content, record_id) in enumerate(zip(rows, record_ids), start=1):
            if record_id in existing_ids:
                continue
            connection.execute(
                "INSERT INTO raw_source_rows "
                "(source_id, source_record_id, source_order, subject_raw, entry_raw, "
                "quality_raw, raw_payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    record_id,
                    position,
                    content.subject_raw,
                    content.entry_raw,
                    content.quality_raw,
                    content.raw_payload_json,
                ),
            )
            existing_ids.add(record_id)
            inserted.append(record_id)

        finished_at = _iso(clock)
        connection.execute(
            "INSERT INTO import_runs "
            "(run_id, source_id, started_at, finished_at, input_file_hash, row_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                resolved_run_id,
                source_id,
                started_at,
                finished_at,
                resolved_input_file_hash,
                row_count,
                RUN_STATUS_COMPLETED,
            ),
        )

        run_row_dicts: List[Dict[str, object]] = []
        for order, record_id in enumerate(record_ids, start=1):
            connection.execute(
                "INSERT INTO run_rows (run_id, source_record_id, source_order) "
                "VALUES (?, ?, ?)",
                (resolved_run_id, record_id, order),
            )
            run_row_dicts.append(
                {"run_id": resolved_run_id, "source_record_id": record_id, "source_order": order}
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    ledger.append_import_run(
        source_id,
        {
            "run_id": resolved_run_id,
            "source_id": source_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "input_file_hash": resolved_input_file_hash,
            "row_count": row_count,
            "status": RUN_STATUS_COMPLETED,
        },
        ledger_root=ledger_root,
    )
    ledger.append_run_rows(source_id, run_row_dicts, ledger_root=ledger_root)

    return RunOutcome(
        run_id=resolved_run_id,
        source_id=source_id,
        status=RUN_STATUS_COMPLETED,
        started_at=started_at,
        finished_at=finished_at,
        input_file_hash=resolved_input_file_hash,
        row_count=row_count,
        inserted_source_record_ids=tuple(inserted),
        run_row_source_record_ids=tuple(record_ids),
    )


def record_failed_run(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    run_id: Optional[str] = None,
    clock: Optional[Clock] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    ledger_root: Path = ledger.DEFAULT_LEDGER_ROOT,
) -> RunOutcome:
    """Record a failed run: metadata only, never rows.

    Takes no ``rows`` argument by design — a failed run cannot insert
    ``raw_source_rows`` or ``run_rows`` under any circumstance. Fields that
    were never determined (``finished_at``, absent an explicit override) are
    left null rather than guessed. The failed run is still mirrored to the
    ledger: recording *that a run failed* is itself a durable operational
    fact, distinct from the raw data it never produced.
    """
    resolved_run_id = run_id or make_run_id(source_id, clock=clock)
    resolved_started_at = started_at or _iso(clock)

    connection.execute(
        "INSERT INTO import_runs "
        "(run_id, source_id, started_at, finished_at, input_file_hash, row_count, status) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
        (resolved_run_id, source_id, resolved_started_at, finished_at, RUN_STATUS_FAILED),
    )
    connection.commit()

    ledger.append_import_run(
        source_id,
        {
            "run_id": resolved_run_id,
            "source_id": source_id,
            "started_at": resolved_started_at,
            "finished_at": finished_at,
            "input_file_hash": None,
            "row_count": None,
            "status": RUN_STATUS_FAILED,
        },
        ledger_root=ledger_root,
    )

    return RunOutcome(
        run_id=resolved_run_id,
        source_id=source_id,
        status=RUN_STATUS_FAILED,
        started_at=resolved_started_at,
        finished_at=finished_at,
        input_file_hash=None,
        row_count=None,
    )


def record_in_progress_run(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    run_id: Optional[str] = None,
    clock: Optional[Clock] = None,
    started_at: Optional[str] = None,
) -> RunOutcome:
    """Record a run that has started but not yet resolved.

    SQLite-only: deliberately not mirrored to the ledger (see module
    docstring) since an in-progress run is transient state, not yet a
    durable historical fact.
    """
    resolved_run_id = run_id or make_run_id(source_id, clock=clock)
    resolved_started_at = started_at or _iso(clock)

    connection.execute(
        "INSERT INTO import_runs "
        "(run_id, source_id, started_at, finished_at, input_file_hash, row_count, status) "
        "VALUES (?, ?, ?, NULL, NULL, NULL, ?)",
        (resolved_run_id, source_id, resolved_started_at, RUN_STATUS_IN_PROGRESS),
    )
    connection.commit()

    return RunOutcome(
        run_id=resolved_run_id,
        source_id=source_id,
        status=RUN_STATUS_IN_PROGRESS,
        started_at=resolved_started_at,
        finished_at=None,
        input_file_hash=None,
        row_count=None,
    )
