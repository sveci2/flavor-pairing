#!/usr/bin/env python3
"""Regenerate the canonical public sample package from the committed
project-owned inputs via the real CP0-CP6 pipeline (CP7).

Input convention: every file in --input-dir is deliberately named
``<source_id>.csv``. The committed inputs are
``data/sample_input/src_synthetic_main_pairing.csv`` and
``data/sample_input/src_synthetic_quality.csv``; each stem must be a source
registered in sources.csv with a public-safe rights_status.
Restricted/unverified sources are refused — sample regeneration processes
only public project-owned demo data.

Pipeline per run, inside a fresh temporary run directory that is always
newly created and removed on success or failure (``--scratch-dir`` names a
*parent* under which the fresh temporary directory is created):

1. fresh SQLite working database;
2. import the decision tables from --decision-dir: sources.csv,
   entities.csv (immutable reviewed input — verified unchanged afterwards
   and never re-exported), entity_source_names.csv (mixed decision table:
   human rows protected by the CP5 merge rule, machine rows regenerated
   deterministically). The import is strict; the one-time
   ``--bootstrap-legacy-mappings`` mode (below) is the only path around a
   legacy duplicated mapping file;
3. ingest each public input file with a fixed deterministic clock; the run
   ledgers live in the run directory — reproducible sample-build ledgers
   are deliberately temporary, unlike the committed operational ledgers
   under data/ledger/ (different purposes);
4. parse (CP4), normalize (CP5), and reconstruct affinities (CP6) per
   source;
5. assemble a COMPLETE package in the run directory: the five
   configuration files and entities.csv copied byte-for-byte from
   --decision-dir, plus the seven exported tables (EXPORTED_TABLES);
6. validate that complete temporary package BEFORE touching the
   destination; any error aborts with nothing copied;
7. then either
   - ``--check``: compare the seven exported files byte-for-byte against
     --output, flag missing/changed files and unexpected stale generated
     CSVs, AND validate --output as a complete canonical package,
     modifying nothing; non-zero on any drift or validation error;
   - normal mode: preflight the destination (reject unexpected CSVs;
     require the passthrough files when --output is the decision directory
     itself — their content validity is already proven by step 6, which
     validated those exact bytes), stage the intended files in a temporary
     sibling directory, then move them into place with os.replace. This is
     **staged per-file atomic replacement**: each individual file replace
     is atomic and all content is fully staged before the first replace,
     but the multi-file sequence as a whole is not transactional — an
     exotic failure mid-sequence could leave some files updated. Into the
     decision directory only the seven exported files are replaced; into
     any other directory the complete self-contained package is written.
     The destination is validated afterwards.

One-time legacy migration (``--bootstrap-legacy-mappings``): the historical
committed entity_source_names.csv contains duplicate
(source_id, source_text, source_role) keys and cannot be imported strictly.
With this flag, the legacy file is read into a temporary migration seed:
rows are grouped by key; duplicates that disagree on any decision-bearing
field (entity_id, normalization_status, notes) abort with a conflict report
for human review; semantically identical duplicates retain one
deterministically selected row (lowest source_name_id). The committed file
is never modified during preprocessing; the deduplicated seed is imported,
the real pipeline runs, and the final deterministic entity_source_names.csv
leaves through the standard export path. A before/after summary (total
rows, unique keys, duplicate rows retired, preserved non-machine statuses)
is reported. After the clean file is committed, ordinary strict
regeneration needs no flag.

No input, decision, output, or scratch path may lie inside
data/imports_private/, and nothing under that path is ever read or
written. No temporary path appears in any exported cell. Runtime is
Python 3.9-compatible standard library plus the flavor_pairing package.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # allow `python3 scripts/regenerate_sample.py`

from flavor_pairing.config.loaders import load_config
from flavor_pairing.ingest import rights
from flavor_pairing.ingest.raw_ingest import ingest_file
from flavor_pairing.normalize.affinities import normalize_affinities
from flavor_pairing.normalize.entities import MACHINE_MAPPING_STATUSES
from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.parse.row_parser import parse_source
from flavor_pairing.store import csv_io, db
from flavor_pairing.validation import (
    CONFIG_FILES,
    EXPECTED_FILES,
    validate_sample_package,
)

DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "sample_input"
DEFAULT_DECISION_DIR = REPO_ROOT / "data" / "sample"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "sample"

PRIVATE_PATH_MARKER = "imports_private"

# Decision tables imported into the working store, in FK-safe order.
IMPORTED_DECISION_TABLES: Tuple[str, ...] = ("sources", "entities", "entity_source_names")

# Files copied verbatim from --decision-dir into the temporary complete
# package (and into a self-contained --output): the five configuration
# files plus the human-owned, never-regenerated entities.csv.
PASSTHROUGH_FILES: Tuple[str, ...] = CONFIG_FILES + ("entities.csv",)

# The seven exported tables (approved CP7 decision 1): six generated tables
# plus the durable mixed mapping table. entities.csv is NEVER exported.
EXPORTED_TABLES: Tuple[str, ...] = (
    "entity_source_names",
    "raw_source_rows",
    "parsed_source_rows",
    "entity_attributes",
    "pairing_observations",
    "affinity_groups",
    "affinity_members",
)

# entity_source_names fields that carry the decision (bootstrap conflict
# detection); source_name_id is identity, not decision content.
MAPPING_DECISION_FIELDS: Tuple[str, ...] = ("entity_id", "normalization_status", "notes")

# Fixed epoch so run IDs/timestamps are deterministic and repeated
# regenerations are byte-identical.
CLOCK_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RegenError(Exception):
    """Sample regeneration cannot proceed safely."""


@dataclass(frozen=True)
class BootstrapSummary:
    """Before/after accounting for the one-time legacy mapping migration."""

    total_rows: int
    unique_keys: int
    duplicates_retired: int
    preserved_statuses: Dict[str, int]


@dataclass
class RegenReport:
    source_ids: List[str]
    export_counts: Dict[str, int]
    drift: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    bootstrap: Optional[BootstrapSummary] = None

    @property
    def ok(self) -> bool:
        return not self.drift and not self.validation_errors


def _fixed_clock(start: datetime = CLOCK_EPOCH, step_seconds: int = 1):
    state = {"t": start}

    def _clock() -> datetime:
        current = state["t"]
        state["t"] = current + timedelta(seconds=step_seconds)
        return current

    return _clock


def _reject_private_path(label: str, path: Optional[Path]) -> None:
    if path is None:
        return
    resolved = Path(path).resolve()
    if PRIVATE_PATH_MARKER in resolved.parts:
        raise RegenError(
            f"{label} path {resolved} lies inside {PRIVATE_PATH_MARKER}; "
            f"sample regeneration never touches private data"
        )


def _discover_inputs(input_dir: Path, config) -> List[Tuple[str, Path]]:
    """(source_id, input_csv) pairs, sorted. Files are deliberately named
    <source_id>.csv; every stem must be a registered, public-safe source."""
    if not input_dir.is_dir():
        raise RegenError(f"input directory not found: {input_dir}")
    pairs: List[Tuple[str, Path]] = []
    for path in sorted(input_dir.glob("*.csv")):
        source_id = path.stem
        if source_id not in config.sources:
            raise RegenError(
                f"{path.name}: file stem '{source_id}' is not a registered "
                f"source_id in sources.csv (input files must be named "
                f"<source_id>.csv)"
            )
        status = config.sources[source_id].rights_status
        if not rights.is_public_safe(status):
            raise RegenError(
                f"{path.name}: source '{source_id}' has rights_status "
                f"'{status}'; sample regeneration processes only public "
                f"project-owned demo data"
            )
        pairs.append((source_id, path))
    if not pairs:
        raise RegenError(f"no <source_id>.csv input files found in {input_dir}")
    return pairs


def _build_bootstrap_seed(
    legacy_path: Path, run_dir: Path
) -> Tuple[Path, BootstrapSummary]:
    """Deduplicate the legacy mapping file into a temporary migration seed.

    The committed file is only read, never modified. Duplicates that agree
    on every decision-bearing field collapse to the row with the lowest
    source_name_id (deterministic); any disagreement aborts for human
    review.
    """
    spec = csv_io.TABLES["entity_source_names"]
    with legacy_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [
            {key: (value or "") for key, value in row.items() if key is not None}
            for row in reader
        ]

    grouped: Dict[Tuple[str, str, str], List[Dict[str, str]]] = {}
    for row in rows:
        key = (row["source_id"], row["source_text"], row["source_role"])
        grouped.setdefault(key, []).append(row)

    retained: List[Dict[str, str]] = []
    duplicates_retired = 0
    for key in sorted(grouped):
        group = sorted(grouped[key], key=lambda row: row["source_name_id"])
        first = group[0]
        for other in group[1:]:
            conflicts = [
                (field_name, first[field_name], other[field_name])
                for field_name in MAPPING_DECISION_FIELDS
                if first[field_name] != other[field_name]
            ]
            if conflicts:
                described = "; ".join(
                    f"{field_name}: {a!r} vs {b!r}" for field_name, a, b in conflicts
                )
                raise RegenError(
                    f"entity_source_names.csv: duplicate rows for key {key} "
                    f"disagree on decision-bearing field(s) ({described}); "
                    f"resolve this conflict by human review before bootstrapping"
                )
        duplicates_retired += len(group) - 1
        retained.append(first)

    seed_path = run_dir / "bootstrap_entity_source_names.csv"
    with seed_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(spec.columns)
        for row in retained:
            writer.writerow([row[column] for column in spec.columns])

    preserved = Counter(
        row["normalization_status"]
        for row in retained
        if row["normalization_status"] not in MACHINE_MAPPING_STATUSES
    )
    summary = BootstrapSummary(
        total_rows=len(rows),
        unique_keys=len(retained),
        duplicates_retired=duplicates_retired,
        preserved_statuses=dict(sorted(preserved.items())),
    )
    return seed_path, summary


def _preflight_destination(output_dir: Path, decision_dir: Path) -> bool:
    """Check the destination BEFORE any copy; returns same_as_decision_dir.

    Rejects unexpected CSVs (stale/unknown outputs) so a failure cannot
    leave the destination partially modified, and confirms the passthrough
    files are present when regenerating in place.
    """
    same = output_dir.resolve() == Path(decision_dir).resolve()
    if output_dir.exists():
        present = {path.name for path in output_dir.glob("*.csv")}
        unexpected = sorted(present - set(EXPECTED_FILES))
        if unexpected:
            raise RegenError(
                f"destination {output_dir} contains unexpected CSV files "
                f"{unexpected}; refusing to modify a directory holding stale "
                f"or unknown outputs"
            )
        if same:
            missing = [name for name in PASSTHROUGH_FILES if name not in present]
            if missing:
                raise RegenError(
                    f"destination {output_dir} is missing passthrough file(s) "
                    f"{missing}; the decision directory must hold the complete "
                    f"configuration and entities.csv"
                )
    elif same:
        raise RegenError(f"decision directory {decision_dir} does not exist")
    return same


def regenerate(
    input_dir: Path,
    decision_dir: Path,
    output_dir: Path,
    *,
    check: bool = False,
    scratch_parent: Optional[Path] = None,
    bootstrap_legacy_mappings: bool = False,
) -> RegenReport:
    """Run the full regeneration; write to output_dir or compare (--check).

    The actual run directory is always a fresh ``TemporaryDirectory`` —
    optionally created under ``scratch_parent`` — and is removed after
    success or failure alike.
    """
    _reject_private_path("input", input_dir)
    _reject_private_path("decision", decision_dir)
    _reject_private_path("output", output_dir)
    _reject_private_path("scratch", scratch_parent)
    if scratch_parent is not None:
        Path(scratch_parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="flavor_pairing_regen_",
        dir=str(scratch_parent) if scratch_parent is not None else None,
    ) as temp_dir:
        return _regenerate_in(
            Path(temp_dir), input_dir, decision_dir, output_dir, check,
            bootstrap_legacy_mappings,
        )


def _regenerate_in(
    run_dir: Path,
    input_dir: Path,
    decision_dir: Path,
    output_dir: Path,
    check: bool,
    bootstrap_legacy_mappings: bool,
) -> RegenReport:
    output_dir = Path(output_dir)
    config = load_config(decision_dir)
    inputs = _discover_inputs(input_dir, config)

    bootstrap_summary: Optional[BootstrapSummary] = None
    mapping_spec = csv_io.TABLES["entity_source_names"]
    mapping_import_path = decision_dir / mapping_spec.template_filename
    if bootstrap_legacy_mappings:
        mapping_import_path, bootstrap_summary = _build_bootstrap_seed(
            decision_dir / mapping_spec.template_filename, run_dir
        )

    connection = db.open_database(run_dir / "working.sqlite")
    try:
        for table in IMPORTED_DECISION_TABLES:
            spec = csv_io.TABLES[table]
            path = (
                mapping_import_path
                if table == "entity_source_names"
                else decision_dir / spec.template_filename
            )
            try:
                csv_io.import_table(connection, spec, path)
            except sqlite3.IntegrityError as exc:
                raise RegenError(
                    f"{spec.template_filename} could not be imported strictly "
                    f"({exc}); if this is the known legacy duplicated mapping "
                    f"file, rerun once with --bootstrap-legacy-mappings"
                ) from exc

        entities_before = [
            tuple(row)
            for row in connection.execute("SELECT * FROM entities ORDER BY entity_id")
        ]

        clock = _fixed_clock()
        for source_id, input_csv in inputs:
            ingest_file(
                config,
                source_id,
                input_csv,
                connection,
                private_ledger_root=run_dir / "ledger_private",
                public_ledger_root=run_dir / "ledger_public",
                clock=clock,
            )
            parse_source(connection, config, source_id)
            normalize_source(connection, config, source_id)
            normalize_affinities(connection, config, source_id)

        entities_after = [
            tuple(row)
            for row in connection.execute("SELECT * FROM entities ORDER BY entity_id")
        ]
        if entities_before != entities_after:
            raise RegenError(
                "entities table changed during regeneration; entities.csv is an "
                "immutable reviewed input and normalization must never alter it"
            )

        # Assemble the COMPLETE package in the run directory.
        package_dir = run_dir / "package"
        package_dir.mkdir()
        for name in PASSTHROUGH_FILES:
            shutil.copyfile(decision_dir / name, package_dir / name)
        export_counts: Dict[str, int] = {}
        for table in EXPORTED_TABLES:
            spec = csv_io.TABLES[table]
            export_counts[spec.template_filename] = csv_io.export_table(
                connection, spec, package_dir / spec.template_filename
            )
    finally:
        connection.close()

    report = RegenReport(
        source_ids=[source_id for source_id, _ in inputs],
        export_counts=export_counts,
        bootstrap=bootstrap_summary,
    )

    # Validate the freshly built complete package BEFORE touching anything.
    result = validate_sample_package(package_dir)
    if result.errors:
        report.validation_errors = list(result.errors)
        return report

    exported_names = [csv_io.TABLES[table].template_filename for table in EXPORTED_TABLES]

    if check:
        for name in exported_names:
            committed = output_dir / name
            if not committed.is_file():
                report.drift.append(f"{name}: missing from {output_dir}")
            elif committed.read_bytes() != (package_dir / name).read_bytes():
                report.drift.append(f"{name}: content differs from regenerated output")
        present = (
            {path.name for path in output_dir.glob("*.csv")}
            if output_dir.exists()
            else set()
        )
        for name in sorted(present - set(EXPECTED_FILES)):
            report.drift.append(
                f"{name}: unexpected CSV in {output_dir} (stale generated file?)"
            )
        # The committed destination must itself be a complete, valid
        # canonical package — not merely byte-equal on the exported files.
        destination = validate_sample_package(output_dir)
        report.validation_errors = list(destination.errors)
        return report

    # Normal mode: preflight the destination, then stage and replace
    # (staged per-file atomic replacement; see module docstring — not
    # transactional across files).
    same_as_decision = _preflight_destination(output_dir, decision_dir)
    names_to_copy = (
        exported_names if same_as_decision else list(PASSTHROUGH_FILES) + exported_names
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(prefix=".regen_stage_", dir=str(output_dir.parent))
    )
    try:
        for name in names_to_copy:
            shutil.copyfile(package_dir / name, stage_dir / name)
        for name in names_to_copy:
            os.replace(stage_dir / name, output_dir / name)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    # Final destination validation.
    final = validate_sample_package(output_dir)
    report.validation_errors = list(final.errors)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR),
                        help="directory of public <source_id>.csv input files")
    parser.add_argument("--decision-dir", default=str(DEFAULT_DECISION_DIR),
                        help="directory of configuration and decision CSVs")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR),
                        help="directory receiving the generated sample tables")
    parser.add_argument("--check", action="store_true",
                        help="regenerate into a temporary directory and compare "
                             "against --output without modifying it; non-zero on drift")
    parser.add_argument("--scratch-dir", default=None,
                        help="optional PARENT directory under which the fresh "
                             "temporary run directory is created (the run "
                             "directory itself is always new and removed)")
    parser.add_argument("--bootstrap-legacy-mappings", action="store_true",
                        help="one-time migration: deduplicate the legacy "
                             "entity_source_names.csv into a temporary seed "
                             "(aborting on conflicting duplicate decisions) "
                             "before regenerating; normal runs are strict")
    args = parser.parse_args(argv)

    try:
        report = regenerate(
            Path(args.input_dir),
            Path(args.decision_dir),
            Path(args.output),
            check=args.check,
            scratch_parent=Path(args.scratch_dir) if args.scratch_dir else None,
            bootstrap_legacy_mappings=args.bootstrap_legacy_mappings,
        )
    except Exception as exc:  # RegenError, ConfigError, IngestError, ...
        print(f"REGENERATION FAILED: {exc}", file=sys.stderr)
        return 1

    if report.bootstrap is not None:
        summary = report.bootstrap
        print(
            f"BOOTSTRAP: total_rows={summary.total_rows} "
            f"unique_keys={summary.unique_keys} "
            f"duplicates_retired={summary.duplicates_retired} "
            f"preserved_statuses={summary.preserved_statuses}"
        )
    for name in sorted(report.export_counts):
        print(f"{name}: {report.export_counts[name]} rows")
    for line in report.drift:
        print(f"DRIFT: {line}", file=sys.stderr)
    for error in report.validation_errors:
        print(f"VALIDATION: {error}", file=sys.stderr)

    if not report.ok:
        print("SAMPLE REGENERATION CHECK FAILED", file=sys.stderr)
        return 1
    print("SAMPLE UP TO DATE" if args.check else "SAMPLE REGENERATED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
