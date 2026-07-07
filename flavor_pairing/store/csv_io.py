"""CSV <-> SQLite import/export under the conventions in
docs/DATA_FOUNDATION_PLAN.md §14.

Table metadata (column order, template filename, sort key) is plain Python
data, defined in this module — never derived by inspecting the database via
SQLite's schema-introspection statements. That keeps every piece of
SQLite-specific behavior confined to ``store/db.py``; this module only ever
issues portable ``SELECT``/``INSERT`` statements built from the metadata
below.

Conventions applied here (§14):

- Encoding: UTF-8 with BOM (``utf-8-sig``) on both read and write.
- Null convention: empty CSV string <-> SQL ``NULL``, uniformly, with no
  per-column special-casing.
- Column order: exactly ``TableSpec.columns``, matching ``data/templates/*.csv``.
- Row order: exports are sorted by ``TableSpec.sort_key``, deterministically.
- Newlines: LF only, via explicit ``lineterminator="\\n"``.
- Quoting: stdlib ``csv`` module defaults (``QUOTE_MINIMAL``).

CP2 scope only: no ingestion, run ledger, parsing, normalization, or review
logic reads or writes through this module.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from flavor_pairing.config.loaders import (
    AFFINITY_SPLIT_RULES_COLUMNS,
    ATTRIBUTE_LABELS_COLUMNS,
    IMPORT_MAPPINGS_COLUMNS,
    SOURCES_COLUMNS,
    STRENGTH_MAPPINGS_COLUMNS,
)

__all__ = ["TableSpec", "TABLES", "TABLE_IMPORT_ORDER", "import_table", "export_table", "import_all", "export_all"]


@dataclass(frozen=True)
class TableSpec:
    """Portable metadata for one table: no SQLite introspection involved."""

    name: str
    template_filename: str
    columns: Tuple[str, ...]
    sort_key: Tuple[str, ...]


ENTITIES_COLUMNS = (
    "entity_id",
    "canonical_name",
    "display_name",
    "entity_type",
    "parent_entity_id",
    "normalization_status",
    "review_status",
    "notes",
)
RAW_SOURCE_ROWS_COLUMNS = (
    "source_id",
    "source_record_id",
    "source_order",
    "subject_raw",
    "entry_raw",
    "quality_raw",
    "raw_payload_json",
)
PARSED_SOURCE_ROWS_COLUMNS = (
    "source_id",
    "source_record_id",
    "row_type",
    "subject_clean",
    "entry_clean",
    "attribute_name",
    "attribute_value_raw",
    "strength_marker_raw",
    "strength_label",
    "strength_score",
    "strength_method",
    "parser_confidence",
    "requires_review",
)
ENTITY_SOURCE_NAMES_COLUMNS = (
    "source_name_id",
    "source_id",
    "source_text",
    "source_role",
    "entity_id",
    "normalization_status",
    "notes",
)
ENTITY_ATTRIBUTES_COLUMNS = (
    "attribute_id",
    "source_id",
    "source_record_id",
    "entity_id",
    "attribute_name",
    "attribute_value_raw",
    "attribute_value_normalized",
    "normalization_method",
    "review_status",
)
PAIRING_OBSERVATIONS_COLUMNS = (
    "observation_id",
    "source_id",
    "source_record_id",
    "subject_entity_id",
    "paired_entity_id",
    "paired_text_raw",
    "strength_label",
    "strength_score",
    "strength_method",
    "normalization_status",
    "review_status",
)
AFFINITY_GROUPS_COLUMNS = (
    "affinity_id",
    "source_id",
    "source_record_id",
    "subject_entity_id",
    "affinity_text_raw",
    "review_status",
)
AFFINITY_MEMBERS_COLUMNS = (
    "affinity_id",
    "member_order",
    "member_entity_id",
    "member_text_raw",
    "normalization_status",
)

TABLES: Dict[str, TableSpec] = {
    "sources": TableSpec("sources", "sources.csv", SOURCES_COLUMNS, ("source_id",)),
    "entities": TableSpec("entities", "entities.csv", ENTITIES_COLUMNS, ("entity_id",)),
    "raw_source_rows": TableSpec(
        "raw_source_rows", "raw_source_rows.csv", RAW_SOURCE_ROWS_COLUMNS,
        ("source_id", "source_record_id"),
    ),
    "parsed_source_rows": TableSpec(
        "parsed_source_rows", "parsed_source_rows.csv", PARSED_SOURCE_ROWS_COLUMNS,
        ("source_id", "source_record_id"),
    ),
    "entity_source_names": TableSpec(
        "entity_source_names", "entity_source_names.csv", ENTITY_SOURCE_NAMES_COLUMNS,
        ("source_name_id",),
    ),
    "entity_attributes": TableSpec(
        "entity_attributes", "entity_attributes.csv", ENTITY_ATTRIBUTES_COLUMNS,
        ("attribute_id",),
    ),
    "pairing_observations": TableSpec(
        "pairing_observations", "pairing_observations.csv", PAIRING_OBSERVATIONS_COLUMNS,
        ("observation_id",),
    ),
    "affinity_groups": TableSpec(
        "affinity_groups", "affinity_groups.csv", AFFINITY_GROUPS_COLUMNS,
        ("affinity_id",),
    ),
    "affinity_members": TableSpec(
        "affinity_members", "affinity_members.csv", AFFINITY_MEMBERS_COLUMNS,
        ("affinity_id", "member_order"),
    ),
    "import_mappings": TableSpec(
        "import_mappings", "import_mappings.csv", IMPORT_MAPPINGS_COLUMNS,
        ("source_format", "target_file", "target_field"),
    ),
    "strength_mappings": TableSpec(
        "strength_mappings", "strength_mappings.csv", STRENGTH_MAPPINGS_COLUMNS,
        ("input_source_format", "marker_key"),
    ),
    "attribute_labels": TableSpec(
        "attribute_labels", "attribute_labels.csv", ATTRIBUTE_LABELS_COLUMNS,
        ("source_format", "source_label"),
    ),
    "affinity_split_rules": TableSpec(
        "affinity_split_rules", "affinity_split_rules.csv", AFFINITY_SPLIT_RULES_COLUMNS,
        ("source_format",),
    ),
}

# FK-safe order: every table appears after every table it references.
TABLE_IMPORT_ORDER: Tuple[str, ...] = (
    "sources",
    "entities",
    "raw_source_rows",
    "parsed_source_rows",
    "entity_source_names",
    "entity_attributes",
    "pairing_observations",
    "affinity_groups",
    "affinity_members",
    "import_mappings",
    "strength_mappings",
    "attribute_labels",
    "affinity_split_rules",
)


def _read_csv_rows(path: Path, columns: Tuple[str, ...]):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [column for column in columns if column not in header]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s) {missing}; "
                f"expected header {list(columns)}, found {list(header)}"
            )
        return [dict(row) for row in reader]


def import_table(connection: sqlite3.Connection, spec: TableSpec, csv_path: Path) -> int:
    """Load one CSV file into its table. Empty strings become SQL NULL."""
    rows = _read_csv_rows(csv_path, spec.columns)
    placeholders = ", ".join("?" for _ in spec.columns)
    column_list = ", ".join(spec.columns)
    values = [
        tuple((row.get(column) or None) for column in spec.columns)
        for row in rows
    ]
    if values:
        connection.executemany(
            f"INSERT INTO {spec.name} ({column_list}) VALUES ({placeholders})",
            values,
        )
        connection.commit()
    return len(values)


def export_table(connection: sqlite3.Connection, spec: TableSpec, csv_path: Path) -> int:
    """Write one table to a canonical CSV file. SQL NULL becomes empty string."""
    column_list = ", ".join(spec.columns)
    order_by = ", ".join(spec.sort_key)
    cursor = connection.execute(f"SELECT {column_list} FROM {spec.name} ORDER BY {order_by}")
    rows = cursor.fetchall()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(spec.columns)
        for row in rows:
            writer.writerow(["" if value is None else str(value) for value in row])
    return len(rows)


def import_all(connection: sqlite3.Connection, csv_dir: Path) -> Dict[str, int]:
    """Load every table's CSV from ``csv_dir``, in FK-safe order."""
    counts: Dict[str, int] = {}
    for name in TABLE_IMPORT_ORDER:
        spec = TABLES[name]
        counts[name] = import_table(connection, spec, csv_dir / spec.template_filename)
    return counts


def export_all(connection: sqlite3.Connection, csv_dir: Path) -> Dict[str, int]:
    """Export every table to a canonical CSV under ``csv_dir``."""
    counts: Dict[str, int] = {}
    for name in TABLE_IMPORT_ORDER:
        spec = TABLES[name]
        counts[name] = export_table(connection, spec, csv_dir / spec.template_filename)
    return counts
