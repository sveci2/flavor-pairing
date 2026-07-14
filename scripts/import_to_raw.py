#!/usr/bin/env python3
"""Thin CLI wrapper around flavor_pairing.ingest.raw_ingest (CP3B).

Converts a supported external CSV into raw_source_rows, via the real
content-derived-identity ingestion pipeline (SQLite working store + durable
CSV ledger) instead of a bespoke positional writer. Column mapping comes
only from import_mappings.csv (docs/DECISIONS.md §E) — this script contains
no per-format branching and no hard-coded column names.

output_csv is a canonical export of raw_source_rows filtered to
--source-id. Because raw_source_rows is an append-only historical table
(docs/SCHEMA.md §2), this is the source's ENTIRE ingestion history across
every run ever recorded against the given --db, not just the rows from this
invocation's input file. It is not filtered to the current version — rows
that a later run removed are still historically preserved here (see
docs/DECISIONS.md §H). This is a deliberate behavior change from the
pre-CP3B script, which mirrored exactly one input file 1:1.

This script intentionally does not normalize ingredient names or scores.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # allow `python3 scripts/import_to_raw.py` from the repo root

from flavor_pairing.config.loaders import ConfigError, load_config
from flavor_pairing.ingest.raw_ingest import IngestError, ingest_file
from flavor_pairing.store import csv_io, db

# Real on-disk default for restricted/unverified sources' ledger. Defined
# only here (not inside flavor_pairing/) per the approved CP3B design: the
# package requires private_ledger_root explicitly from every caller, and
# this script is the one place that supplies the real path for actual CLI
# use. Never exercised by the test suite (tests always pass a tmp_path
# override) and never read/written by this session.
DEFAULT_PRIVATE_LEDGER_ROOT = REPO_ROOT / "data" / "imports_private" / "ledger"
DEFAULT_PUBLIC_LEDGER_ROOT = REPO_ROOT / "data" / "ledger"
DEFAULT_CONFIG_DIR = REPO_ROOT / "data" / "sample"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "build" / "working.sqlite"


def _ensure_source_registered(connection, source) -> None:
    """Bootstrap the working store's sources row for `source` if missing.

    ingest_file() deliberately never writes to `sources` — it's a decision
    table (docs/DECISIONS.md §J), not something raw ingestion populates.
    A fresh working database therefore needs its FK target seeded once
    before ingestion can insert anything; a persistent working database
    across repeated CLI runs must not fail on re-registering the same
    source_id, hence the existence check rather than a bare INSERT.
    """
    exists = connection.execute(
        "SELECT 1 FROM sources WHERE source_id = ?", (source.source_id,)
    ).fetchone()
    if exists is not None:
        return
    connection.execute(
        "INSERT INTO sources "
        "(source_id, source_name, source_format, source_uri, rights_status, allowed_use, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source.source_id,
            source.source_name,
            source.source_format,
            source.source_uri,
            source.rights_status,
            source.allowed_use,
            source.notes,
        ),
    )
    connection.commit()


def _export_source_raw_rows(connection, source_id: str, output_csv: Path) -> int:
    """Canonical export of raw_source_rows for one source (§14 conventions)."""
    spec = csv_io.TABLES["raw_source_rows"]
    column_list = ", ".join(spec.columns)
    order_by = ", ".join(spec.sort_key)
    cursor = connection.execute(
        f"SELECT {column_list} FROM {spec.name} WHERE source_id = ? ORDER BY {order_by}",
        (source_id,),
    )
    rows = cursor.fetchall()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(spec.columns)
        for row in rows:
            writer.writerow(["" if value is None else str(value) for value in row])
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--format",
        default=None,
        help="optional cross-check against the source_format registered in sources.csv; "
        "an error is raised if it doesn't match (column mapping always comes from "
        "import_mappings.csv, never from this flag)",
    )
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="disposable SQLite working store")
    parser.add_argument(
        "--private-ledger-root",
        default=str(DEFAULT_PRIVATE_LEDGER_ROOT),
        help="ledger root used for sources whose rights_status is not public-safe "
        "(gitignored; never committed)",
    )
    parser.add_argument("--public-ledger-root", default=str(DEFAULT_PUBLIC_LEDGER_ROOT))
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config_dir))
        source = config.source(args.source_id)
    except ConfigError as exc:
        parser.error(str(exc))
        return

    if args.format is not None and args.format != source.source_format:
        parser.error(
            f"--format {args.format!r} does not match source_format "
            f"{source.source_format!r} registered for {args.source_id!r} in sources.csv"
        )

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = db.open_database(db_path)
    try:
        _ensure_source_registered(connection, source)
        outcome = ingest_file(
            config,
            args.source_id,
            Path(args.input_csv),
            connection,
            private_ledger_root=Path(args.private_ledger_root),
            public_ledger_root=Path(args.public_ledger_root),
        )
        row_count = _export_source_raw_rows(connection, args.source_id, Path(args.output_csv))
    except IngestError as exc:
        print(f"INGEST FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        connection.close()

    print(
        f"run_id={outcome.run_id} status={outcome.status} "
        f"row_count={outcome.row_count} inserted={len(outcome.inserted_source_record_ids)} "
        f"exported_raw_rows_for_source={row_count}"
    )


if __name__ == "__main__":
    main()
