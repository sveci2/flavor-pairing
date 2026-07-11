"""Mapping-driven raw ingestion of flat tabular external CSVs (CP3B;
docs/DATA_FOUNDATION_PLAN.md §4, §10).

Two layers:

- :func:`read_mapped_csv` — pure file I/O. Reads one external CSV via its
  format's ``import_mappings.csv`` mapping and returns ordered
  :class:`~flavor_pairing.ingest.identity.RawRowContent` rows plus the real
  file's byte-for-byte SHA-256. No database, no rights decisions.
- :func:`ingest_file` — orchestration. Resolves the source's rights-aware
  ledger root, reads the file, and calls into
  :mod:`flavor_pairing.ingest.runs` to record a completed or failed run.

Column mapping comes only from ``import_mappings.csv`` (docs/DECISIONS.md
§E) — this module contains no per-format branching and no hard-coded column
names. Parsing, normalization, typography detection, and strength
resolution are explicitly out of scope here (docs/DATA_FOUNDATION_PLAN.md
§20-21) — this module only ever produces immutable raw rows.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from flavor_pairing.config.loaders import ColumnMapping, ProjectConfig
from flavor_pairing.ingest import rights
from flavor_pairing.ingest.identity import RawRowContent
from flavor_pairing.ingest.runs import Clock, RunOutcome, record_completed_run, record_failed_run
from flavor_pairing.store import ledger

__all__ = ["IngestError", "read_mapped_csv", "ingest_file"]


class IngestError(Exception):
    """An external file could not be read as raw rows for a registered format."""


def _validate_header(fieldnames: Optional[List[str]], mapping: Mapping[str, ColumnMapping], csv_path: Path) -> None:
    header = fieldnames or []
    missing = [
        (target_field, column_mapping.input_column)
        for target_field, column_mapping in mapping.items()
        if column_mapping.required and column_mapping.input_column not in header
    ]
    if missing:
        described = ", ".join(f"{column!r} (for {field})" for field, column in missing)
        raise IngestError(
            f"{csv_path}: missing required column(s) {described}; header found: {header}"
        )


def _row_to_raw_content(mapping: Mapping[str, ColumnMapping], row: Dict[str, object], csv_path: Path) -> RawRowContent:
    if row.get(None):
        raise IngestError(
            f"{csv_path}: row has more values than the header declares "
            f"(ragged line); extra values {row[None]!r} would otherwise be lost"
        )

    def field_value(target_field: str) -> Optional[str]:
        column_mapping = mapping.get(target_field)
        if column_mapping is None or column_mapping.input_column is None:
            return None
        value = row.get(column_mapping.input_column)
        return None if value is None else str(value)

    subject_raw = field_value("subject_raw")
    entry_raw = field_value("entry_raw")
    # A short/ragged row (fewer cells than the header) gets None from
    # DictReader for a missing trailing column; treat that the same as a
    # blank cell rather than inventing text, but keep the required NOT NULL
    # columns as real strings.
    subject_raw = "" if subject_raw is None else subject_raw
    entry_raw = "" if entry_raw is None else entry_raw

    quality_raw = field_value("quality_raw")
    quality_raw = quality_raw or None  # blank cell -> None (project null convention)

    raw_payload_json = json.dumps(row, ensure_ascii=False, separators=(",", ":"))

    return RawRowContent(
        subject_raw=subject_raw,
        entry_raw=entry_raw,
        quality_raw=quality_raw,
        raw_payload_json=raw_payload_json,
    )


def read_mapped_csv(
    csv_path: Path, mapping: Mapping[str, ColumnMapping]
) -> Tuple[List[RawRowContent], str]:
    """Read one external CSV into ordered ``RawRowContent`` rows plus its file hash.

    - Reads with ``utf-8-sig`` (tolerates a BOM; doesn't require one).
    - ``subject_raw``/``entry_raw`` are copied exactly from their mapped
      column — no trim, no case-fold.
    - ``quality_raw`` is ``None`` when the format has no such column
      (``input_column`` is ``(not present)``) and also when the cell is
      blank (project null convention: blank cell -> NULL, uniformly).
    - ``raw_payload_json`` captures the *entire* original row — every
      source column, not only the mapped ones — as deterministic JSON
      (``ensure_ascii=False``, minimal separators), so no original column
      is ever lost and re-reading the same file reproduces byte-identical
      payloads.
    - A row with more cells than the header (a ragged line) raises
      ``IngestError`` rather than silently dropping the extra values.
    """
    if not csv_path.is_file():
        raise IngestError(f"{csv_path}: input file not found")

    raw_bytes = csv_path.read_bytes()
    input_file_hash = hashlib.sha256(raw_bytes).hexdigest()

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _validate_header(reader.fieldnames, mapping, csv_path)
        rows = [_row_to_raw_content(mapping, row, csv_path) for row in reader]

    return rows, input_file_hash


def ingest_file(
    config: ProjectConfig,
    source_id: str,
    csv_path: Path,
    connection: sqlite3.Connection,
    *,
    private_ledger_root: Path,
    public_ledger_root: Path = ledger.DEFAULT_LEDGER_ROOT,
    run_id: Optional[str] = None,
    clock: Optional[Clock] = None,
) -> RunOutcome:
    """Ingest one external flat-tabular CSV file for ``source_id`` as one run.

    - ``source_id`` must already be registered in ``sources.csv``
      (``config.source`` raises ``ConfigError`` otherwise — no run can be
      recorded, since no rights_status is known yet).
    - The ledger root is resolved from the source's ``rights_status``
      (:mod:`flavor_pairing.ingest.rights`) before the file is touched.
    - Any failure while reading/mapping the file (missing format mapping,
      missing file, missing required column, ragged row) is recorded as a
      failed run (metadata only — no raw rows, no run_rows) and the
      original exception is re-raised.
    - On success, delegates to
      :func:`flavor_pairing.ingest.runs.record_completed_run`, which is
      itself all-or-nothing and append-only against raw data.

    ``private_ledger_root`` has no default — callers must always supply it
    explicitly (see module docs in ``flavor_pairing.ingest.rights``).
    """
    source = config.source(source_id)
    ledger_root = rights.resolve_ledger_root(
        source.rights_status, private_root=private_ledger_root, public_root=public_ledger_root
    )

    try:
        mapping = config.mapping_for(source.source_format)
        rows, input_file_hash = read_mapped_csv(csv_path, mapping)
    except Exception:
        record_failed_run(connection, source_id, run_id=run_id, clock=clock, ledger_root=ledger_root)
        raise

    return record_completed_run(
        connection,
        source_id,
        rows,
        run_id=run_id,
        clock=clock,
        ledger_root=ledger_root,
        input_file_hash=input_file_hash,
    )
