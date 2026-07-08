"""Append-only CSV ledger helpers for ``import_runs``/``run_rows``
(docs/SCHEMA.md §11; docs/DATA_FOUNDATION_PLAN.md §3).

Helper level only: this module knows how to read, append, and rebuild-into-
SQLite the ledger CSVs. It never decides *when* a run happened or *what*
its outcome was — that orchestration lives in
:mod:`flavor_pairing.ingest.runs`.

CP3A scope only: public/project-owned ledger paths
(``data/ledger/<source_id>/``). The gitignored private-source ledger path
described in docs/SCHEMA.md §11 and rights enforcement are not implemented
here.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Tuple

__all__ = [
    "DEFAULT_LEDGER_ROOT",
    "IMPORT_RUNS_LEDGER_COLUMNS",
    "RUN_ROWS_LEDGER_COLUMNS",
    "append_import_run",
    "append_run_rows",
    "ledger_paths",
    "load_ledger_into_db",
    "read_import_runs",
    "read_run_rows",
]

DEFAULT_LEDGER_ROOT = Path("data") / "ledger"

IMPORT_RUNS_LEDGER_COLUMNS: Tuple[str, ...] = (
    "run_id",
    "source_id",
    "started_at",
    "finished_at",
    "input_file_hash",
    "row_count",
    "status",
)
RUN_ROWS_LEDGER_COLUMNS: Tuple[str, ...] = ("run_id", "source_record_id", "source_order")


def ledger_paths(source_id: str, ledger_root: Path = DEFAULT_LEDGER_ROOT) -> Tuple[Path, Path]:
    """The ``(import_runs.csv, run_rows.csv)`` paths for one source."""
    source_dir = Path(ledger_root) / source_id
    return source_dir / "import_runs.csv", source_dir / "run_rows.csv"


def _append_rows(path: Path, columns: Tuple[str, ...], rows: List[Dict[str, object]]) -> None:
    """Append rows to a ledger CSV, creating it (with header + BOM) if needed.

    The BOM is only ever written on file creation. Reopening an *existing*
    file with ``encoding="utf-8-sig"`` would re-emit a BOM at the current
    write position (the codec has no memory of prior opens), corrupting the
    file — so appends to an existing file use plain ``utf-8``, matching the
    encoding already on disk from creation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    encoding = "utf-8-sig" if is_new else "utf-8"
    with path.open("a", newline="", encoding=encoding) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, lineterminator="\n", quoting=csv.QUOTE_MINIMAL
        )
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: ("" if row.get(column) is None else row.get(column)) for column in columns}
            )


def append_import_run(
    source_id: str, row: Dict[str, object], ledger_root: Path = DEFAULT_LEDGER_ROOT
) -> None:
    """Append one row to ``import_runs.csv`` for ``source_id``."""
    import_runs_path, _ = ledger_paths(source_id, ledger_root)
    _append_rows(import_runs_path, IMPORT_RUNS_LEDGER_COLUMNS, [row])


def append_run_rows(
    source_id: str, rows: List[Dict[str, object]], ledger_root: Path = DEFAULT_LEDGER_ROOT
) -> None:
    """Append rows to ``run_rows.csv`` for ``source_id``. No-op if ``rows`` is empty."""
    if not rows:
        return
    _, run_rows_path = ledger_paths(source_id, ledger_root)
    _append_rows(run_rows_path, RUN_ROWS_LEDGER_COLUMNS, rows)


def _read_rows(path: Path, columns: Tuple[str, ...]) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{column: (row.get(column) or "") for column in columns} for row in reader]


def read_import_runs(
    source_id: str, ledger_root: Path = DEFAULT_LEDGER_ROOT
) -> List[Dict[str, str]]:
    """All ``import_runs`` ledger rows for ``source_id``, in file order."""
    import_runs_path, _ = ledger_paths(source_id, ledger_root)
    return _read_rows(import_runs_path, IMPORT_RUNS_LEDGER_COLUMNS)


def read_run_rows(source_id: str, ledger_root: Path = DEFAULT_LEDGER_ROOT) -> List[Dict[str, str]]:
    """All ``run_rows`` ledger rows for ``source_id``, in file order."""
    _, run_rows_path = ledger_paths(source_id, ledger_root)
    return _read_rows(run_rows_path, RUN_ROWS_LEDGER_COLUMNS)


def load_ledger_into_db(
    connection, source_id: str, ledger_root: Path = DEFAULT_LEDGER_ROOT
) -> Dict[str, int]:
    """Rebuild ``import_runs``/``run_rows`` for one source from its ledger CSVs.

    CP3A scope: reconstructs only the ledger-backed tables. Rebuilding
    ``raw_source_rows`` itself is out of scope here (there is no full
    raw-ingestion ledger yet); callers restore it the same way as any other
    table — via :mod:`flavor_pairing.store.csv_io` from its own canonical
    export.
    """
    import_runs_rows = read_import_runs(source_id, ledger_root)
    run_rows_rows = read_run_rows(source_id, ledger_root)

    for row in import_runs_rows:
        connection.execute(
            "INSERT INTO import_runs "
            "(run_id, source_id, started_at, finished_at, input_file_hash, row_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["run_id"],
                row["source_id"],
                row["started_at"],
                row["finished_at"] or None,
                row["input_file_hash"] or None,
                int(row["row_count"]) if row["row_count"] else None,
                row["status"],
            ),
        )
    for row in run_rows_rows:
        connection.execute(
            "INSERT INTO run_rows (run_id, source_record_id, source_order) VALUES (?, ?, ?)",
            (row["run_id"], row["source_record_id"], int(row["source_order"])),
        )
    connection.commit()
    return {"import_runs": len(import_runs_rows), "run_rows": len(run_rows_rows)}
