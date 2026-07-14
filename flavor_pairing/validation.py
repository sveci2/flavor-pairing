"""Sample-package validation (CP7; docs/DATA_FOUNDATION_PLAN.md §15;
docs/DECISIONS.md §B, §D, §F).

Validates one on-disk CSV package directory (``data/sample/`` or a
regenerated copy). Files are validated according to their documented role —
never blindly with one rule set:

- **Configuration files** (``CONFIG_FILES``) — human-authored parser/import
  rules, validated by the CP1 loaders (``load_config``) plus exact header
  checks against the loaders' documented column constants.
- **Decision tables** (``DECISION_FILES``) — ``entities.csv`` (human-owned,
  never regenerated) and ``entity_source_names.csv`` (mixed: protected
  human rows plus deterministic machine rows). Uniqueness and referential
  rules apply strictly, but *status vocabularies are lenient here*: the
  CP5 fail-closed rule treats unrecognized legacy statuses as human-owned,
  so they are permitted only in these two files.
- **Generated tables** (``GENERATED_FILES``) — pipeline output, never
  hand-edited. ``review_status`` is strict. ``normalization_status`` on
  derived rows is either a documented machine status **or** the exact
  status copied verbatim from the driving ``entity_source_names`` mapping
  (CP5/CP6 deliberately propagate protected legacy statuses); an
  undocumented status that does not trace to its driving mapping is an
  error — derived rows never invent status values independently.

Completeness is checked both ways: every expected file must exist, and any
unexpected ``*.csv`` in the package directory (e.g. a stale output from an
older pipeline) is an error rather than silently ignored.

``canonical_package=True`` (the default) additionally enforces the
**canonical-sample-package invariant**: parsed rows and raw rows have
exactly equal key sets. This holds for a fresh single-run sample build and
is deliberately *not* a general storage invariant — a long-lived working
database legitimately contains historical raw rows outside the current
version (docs/DECISIONS.md §H). Pass ``canonical_package=False`` when
validating such a package.

Every rule here is grounded in docs/SCHEMA.md, docs/DECISIONS.md, or an
invariant established by CP1-CP6 code and tests; no speculative rules.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from flavor_pairing.config.loaders import (
    AFFINITY_SPLIT_RULES_COLUMNS,
    ATTRIBUTE_LABELS_COLUMNS,
    IMPORT_MAPPINGS_COLUMNS,
    SOURCES_COLUMNS,
    STRENGTH_MAPPINGS_COLUMNS,
    ConfigError,
    ProjectConfig,
    load_config,
)
from flavor_pairing.normalize.entities import (
    ENTITY_TYPES,
    NORMALIZATION_STATUS_AUTO_MAPPED,
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    REVIEW_STATUSES,
    REVIEW_STATUS_REJECTED,
    ROLE_AFFINITY_MEMBER,
    ROLE_PAIRING_ENTRY,
)
from flavor_pairing.parse.row_parser import (
    PARSER_CONFIDENCES,
    ROW_TYPES,
    ROW_TYPE_AFFINITY_GROUP,
    ROW_TYPE_ATTRIBUTE,
    ROW_TYPE_PAIRING_CANDIDATE,
)
from flavor_pairing.parse.strength import (
    STRENGTH_METHODS,
    STRENGTH_METHOD_UNAVAILABLE,
)
from flavor_pairing.store.csv_io import TABLES

__all__ = [
    "CONFIG_FILES",
    "DECISION_FILES",
    "EXPECTED_FILES",
    "GENERATED_FILES",
    "ValidationResult",
    "validate_sample_package",
]

# File roles (approved CP7 decision 7). entity_source_names.csv is a *mixed*
# decision table: human-owned rows are protected and survive regeneration
# byte-equivalently; machine rows are deterministically written under the
# CP5 merge rule. entities.csv is human-owned and never regenerated.
CONFIG_FILES: Tuple[str, ...] = (
    "sources.csv",
    "import_mappings.csv",
    "strength_mappings.csv",
    "attribute_labels.csv",
    "affinity_split_rules.csv",
)
DECISION_FILES: Tuple[str, ...] = ("entities.csv", "entity_source_names.csv")
GENERATED_FILES: Tuple[str, ...] = (
    "raw_source_rows.csv",
    "parsed_source_rows.csv",
    "entity_attributes.csv",
    "pairing_observations.csv",
    "affinity_groups.csv",
    "affinity_members.csv",
)
EXPECTED_FILES: Tuple[str, ...] = CONFIG_FILES + DECISION_FILES + GENERATED_FILES

# Exact expected headers. Configuration files use the CP1 loaders' documented
# column constants directly; data tables use the store's TableSpec columns.
# Built explicitly so a missing specification is a startup failure here, not
# a KeyError mid-validation.
EXPECTED_HEADERS: Dict[str, Tuple[str, ...]] = {
    "sources.csv": SOURCES_COLUMNS,
    "import_mappings.csv": IMPORT_MAPPINGS_COLUMNS,
    "strength_mappings.csv": STRENGTH_MAPPINGS_COLUMNS,
    "attribute_labels.csv": ATTRIBUTE_LABELS_COLUMNS,
    "affinity_split_rules.csv": AFFINITY_SPLIT_RULES_COLUMNS,
}
for _spec in TABLES.values():
    EXPECTED_HEADERS.setdefault(_spec.template_filename, _spec.columns)
_MISSING_SPECS = [name for name in EXPECTED_FILES if name not in EXPECTED_HEADERS]
if _MISSING_SPECS:  # pragma: no cover - guards future spec drift
    raise RuntimeError(f"no header specification for: {_MISSING_SPECS}")

# Documented status vocabularies (docs/DECISIONS.md §J; plan §7).
DOCUMENTED_NORMALIZATION_STATUSES = frozenset(
    {
        NORMALIZATION_STATUS_UNRESOLVED,
        NORMALIZATION_STATUS_AUTO_MAPPED,
        NORMALIZATION_STATUS_HUMAN_MAPPED,
    }
)

PRIVATE_PATH_MARKER = "imports_private"


@dataclass(frozen=True)
class ValidationResult:
    errors: List[str]
    counts: Dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_table(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = [
            {key: (value or "") for key, value in row.items() if key is not None}
            for row in reader
        ]
    return header, rows


def _check_unique(
    errors: List[str], file_name: str, rows: Sequence[Dict[str, str]],
    key_fields: Tuple[str, ...],
) -> None:
    seen: Set[Tuple[str, ...]] = set()
    for index, row in enumerate(rows, start=2):
        key = tuple(row[field] for field in key_fields)
        if any(not part for part in key):
            errors.append(
                f"{file_name} line {index}: key field(s) {key_fields} must not be blank"
            )
            continue
        if key in seen:
            errors.append(
                f"{file_name} line {index}: duplicate key {dict(zip(key_fields, key))}"
            )
        seen.add(key)


def _safe_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parent_cycle_errors(entities: Sequence[Dict[str, str]]) -> List[str]:
    """Referential + acyclicity check for parent_entity_id (docs/DECISIONS.md §D)."""
    errors: List[str] = []
    parent: Dict[str, str] = {}
    ids = {row["entity_id"] for row in entities}
    for row in entities:
        if row["parent_entity_id"]:
            if row["parent_entity_id"] not in ids:
                errors.append(
                    f"entities.csv: entity '{row['entity_id']}' has unknown "
                    f"parent_entity_id '{row['parent_entity_id']}'"
                )
            else:
                parent[row["entity_id"]] = row["parent_entity_id"]
    for start in parent:
        seen = {start}
        current = start
        while current in parent:
            current = parent[current]
            if current in seen:
                errors.append(
                    f"entities.csv: parent_entity_id cycle involving '{start}'"
                )
                break
            seen.add(current)
    return errors


def _derived_status_error(
    file_name: str,
    row_key: str,
    status: str,
    driving_mapping: Optional[Dict[str, str]],
) -> Optional[str]:
    """Validate one derived row's normalization_status.

    Accepted: a documented machine status, or the exact status copied
    verbatim from the driving entity_source_names mapping (CP5/CP6
    propagate protected legacy statuses deliberately). An undocumented
    status with no matching protected mapping is an invention and fails.
    """
    if not status or status in DOCUMENTED_NORMALIZATION_STATUSES:
        return None
    if driving_mapping is not None and driving_mapping["normalization_status"] == status:
        return None
    return (
        f"{file_name}: {row_key} has normalization_status '{status}' that is "
        f"neither documented nor traceable to its driving entity_source_names "
        f"mapping (derived rows never invent statuses)"
    )


def validate_sample_package(
    sample_dir: Path, *, canonical_package: bool = True
) -> ValidationResult:
    """Validate one CSV package directory; returns errors and table counts."""
    sample_dir = Path(sample_dir)
    errors: List[str] = []
    counts: Dict[str, int] = {}

    # --- Completeness, both ways -----------------------------------------
    present = {path.name for path in sample_dir.glob("*.csv")}
    for name in EXPECTED_FILES:
        if name not in present:
            errors.append(f"{name}: required file is missing")
    for name in sorted(present - set(EXPECTED_FILES)):
        errors.append(
            f"{name}: unexpected CSV in the package directory; not part of the "
            f"documented file set (stale output from an older pipeline?)"
        )
    if errors:
        return ValidationResult(errors=errors, counts=counts)

    # --- Headers exactly match the specifications ------------------------
    tables: Dict[str, List[Dict[str, str]]] = {}
    for name in EXPECTED_FILES:
        header, rows = _read_table(sample_dir / name)
        expected = list(EXPECTED_HEADERS[name])
        if header != expected:
            errors.append(
                f"{name}: header {header} does not exactly match the "
                f"specification {expected}"
            )
            continue
        tables[name] = rows
        counts[name] = len(rows)
    if errors:
        return ValidationResult(errors=errors, counts=counts)

    # --- Configuration files: validated by the CP1 loaders ---------------
    config: Optional[ProjectConfig] = None
    try:
        config = load_config(sample_dir)
    except ConfigError as exc:
        errors.append(f"configuration: {exc}")

    sources = tables["sources.csv"]
    entities = tables["entities.csv"]
    mappings = tables["entity_source_names.csv"]
    raw = tables["raw_source_rows.csv"]
    parsed = tables["parsed_source_rows.csv"]
    attributes = tables["entity_attributes.csv"]
    observations = tables["pairing_observations.csv"]
    groups = tables["affinity_groups.csv"]
    members = tables["affinity_members.csv"]

    # --- Primary keys and documented composite uniqueness ----------------
    _check_unique(errors, "entities.csv", entities, ("entity_id",))
    _check_unique(errors, "raw_source_rows.csv", raw, ("source_record_id",))
    _check_unique(errors, "raw_source_rows.csv", raw, ("source_id", "source_record_id"))
    _check_unique(errors, "parsed_source_rows.csv", parsed, ("source_id", "source_record_id"))
    _check_unique(errors, "entity_attributes.csv", attributes, ("attribute_id",))
    _check_unique(errors, "pairing_observations.csv", observations, ("observation_id",))
    _check_unique(errors, "pairing_observations.csv", observations, ("source_id", "source_record_id"))
    _check_unique(errors, "affinity_groups.csv", groups, ("affinity_id",))
    _check_unique(errors, "affinity_groups.csv", groups, ("source_id", "source_record_id"))
    _check_unique(errors, "affinity_members.csv", members, ("affinity_id", "member_order"))
    _check_unique(errors, "entity_source_names.csv", mappings, ("source_name_id",))
    # docs/DECISIONS.md §A: unique mapping per (source_id, source_text, source_role).
    _check_unique(errors, "entity_source_names.csv", mappings, ("source_id", "source_text", "source_role"))

    # --- Referential integrity --------------------------------------------
    source_ids = {row["source_id"] for row in sources}
    entity_ids = {row["entity_id"] for row in entities}
    raw_keys = {(row["source_id"], row["source_record_id"]) for row in raw}
    parsed_keys = {(row["source_id"], row["source_record_id"]) for row in parsed}
    group_ids = {row["affinity_id"] for row in groups}
    entities_by_id = {row["entity_id"]: row for row in entities}
    mappings_by_key = {
        (row["source_id"], row["source_text"], row["source_role"]): row
        for row in mappings
    }

    for row in raw:
        if row["source_id"] not in source_ids:
            errors.append(
                f"raw_source_rows.csv: record '{row['source_record_id']}' references "
                f"unknown source '{row['source_id']}'"
            )
        if not row["subject_raw"] or not row["entry_raw"]:
            errors.append(
                f"raw_source_rows.csv: record '{row['source_record_id']}' is missing "
                f"required subject_raw/entry_raw"
            )

    for file_name, rows in (
        ("parsed_source_rows.csv", parsed),
        ("entity_attributes.csv", attributes),
        ("pairing_observations.csv", observations),
        ("affinity_groups.csv", groups),
    ):
        for row in rows:
            if (row["source_id"], row["source_record_id"]) not in raw_keys:
                errors.append(
                    f"{file_name}: ({row['source_id']}, {row['source_record_id']}) "
                    f"does not trace to any raw_source_rows record"
                )

    if canonical_package and parsed_keys != raw_keys:
        # Canonical-sample-package invariant only (fresh single-run build):
        # a general working store may hold historical raw rows outside the
        # current version (docs/DECISIONS.md §H).
        errors.append(
            "canonical sample package: parsed_source_rows keys must exactly equal "
            "raw_source_rows keys "
            f"(raw-only: {sorted(raw_keys - parsed_keys)}, "
            f"parsed-only: {sorted(parsed_keys - raw_keys)})"
        )

    for row in entities:
        if row["entity_type"] and row["entity_type"] not in ENTITY_TYPES:
            errors.append(
                f"entities.csv: entity '{row['entity_id']}' has undocumented "
                f"entity_type '{row['entity_type']}'"
            )
    errors.extend(_parent_cycle_errors(entities))

    for row in mappings:
        if row["source_id"] not in source_ids:
            errors.append(
                f"entity_source_names.csv: '{row['source_name_id']}' references "
                f"unknown source '{row['source_id']}'"
            )
        if row["entity_id"] and row["entity_id"] not in entity_ids:
            errors.append(
                f"entity_source_names.csv: '{row['source_name_id']}' references "
                f"unknown entity '{row['entity_id']}'"
            )
        status = row["normalization_status"]
        # Machine-owned statuses must be internally consistent; any other
        # value is human-owned/legacy and permitted here (fail-closed rule)
        # — decision tables only.
        if status == NORMALIZATION_STATUS_AUTO_MAPPED and not row["entity_id"]:
            errors.append(
                f"entity_source_names.csv: '{row['source_name_id']}' is auto_mapped "
                f"but has no entity_id"
            )
        if status == NORMALIZATION_STATUS_UNRESOLVED and row["entity_id"]:
            errors.append(
                f"entity_source_names.csv: '{row['source_name_id']}' is unresolved "
                f"but references entity '{row['entity_id']}'"
            )
        if status == NORMALIZATION_STATUS_AUTO_MAPPED and row["entity_id"]:
            target = entities_by_id.get(row["entity_id"])
            if target is not None and target["review_status"] == REVIEW_STATUS_REJECTED:
                errors.append(
                    f"entity_source_names.csv: machine-owned mapping "
                    f"'{row['source_name_id']}' references rejected entity "
                    f"'{row['entity_id']}' (rejected entities are never "
                    f"auto-selected)"
                )

    # --- Parsed rows: enums and strength consistency ----------------------
    parsed_by_key = {(row["source_id"], row["source_record_id"]): row for row in parsed}
    for row in parsed:
        if row["row_type"] not in ROW_TYPES:
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"undocumented row_type '{row['row_type']}'"
            )
        if row["parser_confidence"] not in PARSER_CONFIDENCES:
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"undocumented parser_confidence '{row['parser_confidence']}'"
            )
        if row["requires_review"] not in ("0", "1"):
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"requires_review '{row['requires_review']}' (must be 0 or 1)"
            )
        score = row["strength_score"]
        if score and score not in ("1", "2", "3", "4"):
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"invalid strength_score '{score}'"
            )
        if bool(row["strength_label"]) != bool(score):
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"strength_label/strength_score mismatch (both or neither)"
            )
        method = row["strength_method"]
        if method and method not in STRENGTH_METHODS:
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"undocumented strength_method '{method}'"
            )
        if method == STRENGTH_METHOD_UNAVAILABLE and score:
            errors.append(
                f"parsed_source_rows.csv: record '{row['source_record_id']}' has "
                f"strength_method 'unavailable' but a strength_score"
            )

    # Anti-fabrication, keyed on source_format (docs/DECISIONS.md §F): in a
    # format whose 'plain' marker carries no score, any scored parsed row
    # must carry marker evidence.
    if config is not None:
        format_by_source = {s.source_id: s.source_format for s in config.sources.values()}
        lossy_formats = {
            source_format
            for source_format, per_format in config.strength_mappings.items()
            if "plain" in per_format and per_format["plain"].normalized_score is None
        }
        for row in parsed:
            source_format = format_by_source.get(row["source_id"])
            if source_format in lossy_formats and row["strength_score"]:
                if not row["strength_marker_raw"]:
                    errors.append(
                        f"parsed_source_rows.csv: record '{row['source_record_id']}' "
                        f"carries a strength_score without marker evidence in "
                        f"typography-lossy format '{source_format}'"
                    )

    # --- Derived tables: strict review_status; row-type discipline --------
    for file_name, rows in (
        ("entity_attributes.csv", attributes),
        ("pairing_observations.csv", observations),
        ("affinity_groups.csv", groups),
    ):
        for row in rows:
            if row["review_status"] not in REVIEW_STATUSES:
                errors.append(
                    f"{file_name}: review_status '{row['review_status']}' is outside "
                    f"the documented vocabulary (generated rows are strict)"
                )

    for row in attributes:
        parsed_row = parsed_by_key.get((row["source_id"], row["source_record_id"]))
        if parsed_row is not None and parsed_row["row_type"] != ROW_TYPE_ATTRIBUTE:
            errors.append(
                f"entity_attributes.csv: '{row['attribute_id']}' derives from a "
                f"'{parsed_row['row_type']}' row, not an attribute row"
            )
        if row["entity_id"] and row["entity_id"] not in entity_ids:
            errors.append(
                f"entity_attributes.csv: '{row['attribute_id']}' references unknown "
                f"entity '{row['entity_id']}'"
            )

    for row in observations:
        if not row["subject_entity_id"]:
            errors.append(
                f"pairing_observations.csv: '{row['observation_id']}' has no "
                f"subject_entity_id (unresolved subjects are skipped, never stored)"
            )
        elif row["subject_entity_id"] not in entity_ids:
            errors.append(
                f"pairing_observations.csv: '{row['observation_id']}' references "
                f"unknown subject entity '{row['subject_entity_id']}'"
            )
        if row["paired_entity_id"] and row["paired_entity_id"] not in entity_ids:
            errors.append(
                f"pairing_observations.csv: '{row['observation_id']}' references "
                f"unknown paired entity '{row['paired_entity_id']}'"
            )
        # normalization_status: documented, or copied verbatim from the
        # driving pairing_entry mapping (protected legacy propagation).
        driving = mappings_by_key.get(
            (row["source_id"], row["paired_text_raw"], ROLE_PAIRING_ENTRY)
        )
        status_error = _derived_status_error(
            "pairing_observations.csv",
            f"'{row['observation_id']}'",
            row["normalization_status"],
            driving,
        )
        if status_error:
            errors.append(status_error)
        parsed_row = parsed_by_key.get((row["source_id"], row["source_record_id"]))
        if parsed_row is not None:
            if parsed_row["row_type"] != ROW_TYPE_PAIRING_CANDIDATE:
                errors.append(
                    f"pairing_observations.csv: '{row['observation_id']}' derives "
                    f"from a '{parsed_row['row_type']}' row — affinity/attribute "
                    f"rows are never flattened into pairing observations"
                )
            for field in ("strength_label", "strength_score", "strength_method"):
                if row[field] != parsed_row[field]:
                    errors.append(
                        f"pairing_observations.csv: '{row['observation_id']}' "
                        f"{field} '{row[field]}' differs from the parsed row's "
                        f"'{parsed_row[field]}' (strength is copied, never "
                        f"reinterpreted)"
                    )

    # --- Affinity groups and members ---------------------------------------
    groups_by_id = {row["affinity_id"]: row for row in groups}
    members_by_group: Dict[str, List[Dict[str, str]]] = {}
    for index, row in enumerate(members, start=2):
        members_by_group.setdefault(row["affinity_id"], []).append(row)
        group = groups_by_id.get(row["affinity_id"])
        if group is None:
            errors.append(
                f"affinity_members.csv line {index}: references unknown "
                f"affinity_id '{row['affinity_id']}'"
            )
        if row["member_entity_id"] and row["member_entity_id"] not in entity_ids:
            errors.append(
                f"affinity_members.csv line {index}: references unknown entity "
                f"'{row['member_entity_id']}'"
            )
        if _safe_int(row["member_order"]) is None:
            errors.append(
                f"affinity_members.csv line {index}: member_order "
                f"'{row['member_order']}' is not an integer"
            )
        # normalization_status: documented, or copied verbatim from the
        # driving affinity_member mapping (protected legacy propagation).
        driving = (
            mappings_by_key.get(
                (group["source_id"], row["member_text_raw"], ROLE_AFFINITY_MEMBER)
            )
            if group is not None
            else None
        )
        status_error = _derived_status_error(
            "affinity_members.csv",
            f"member of '{row['affinity_id']}' (order {row['member_order']})",
            row["normalization_status"],
            driving,
        )
        if status_error:
            errors.append(status_error)

    if config is not None:
        format_by_source = {s.source_id: s.source_format for s in config.sources.values()}
        for group in groups:
            parsed_row = parsed_by_key.get((group["source_id"], group["source_record_id"]))
            if parsed_row is not None and parsed_row["row_type"] != ROW_TYPE_AFFINITY_GROUP:
                errors.append(
                    f"affinity_groups.csv: '{group['affinity_id']}' derives from a "
                    f"'{parsed_row['row_type']}' row, not an affinity_group row"
                )
            if group["subject_entity_id"] and group["subject_entity_id"] not in entity_ids:
                errors.append(
                    f"affinity_groups.csv: '{group['affinity_id']}' references "
                    f"unknown subject entity '{group['subject_entity_id']}'"
                )
            source_format = format_by_source.get(group["source_id"])
            if source_format is None:
                continue
            try:
                delimiter = config.require_affinity_rule(source_format).member_delimiter
            except ConfigError as exc:
                errors.append(f"affinity_groups.csv: '{group['affinity_id']}': {exc}")
                continue
            expected_tokens = group["affinity_text_raw"].split(delimiter)
            group_members = members_by_group.get(group["affinity_id"], [])
            parsed_orders = [_safe_int(m["member_order"]) for m in group_members]
            if any(order is None for order in parsed_orders):
                continue  # already reported as a malformed member_order above
            ordered = sorted(zip(parsed_orders, group_members), key=lambda pair: pair[0])
            orders = [order for order, _ in ordered]
            if orders != list(range(1, len(expected_tokens) + 1)):
                errors.append(
                    f"affinity_groups.csv: '{group['affinity_id']}' member_order "
                    f"sequence {orders} is not exactly 1..{len(expected_tokens)}"
                )
            elif [m["member_text_raw"] for _, m in ordered] != expected_tokens:
                errors.append(
                    f"affinity_groups.csv: '{group['affinity_id']}' member tokens "
                    f"{[m['member_text_raw'] for _, m in ordered]} do not equal "
                    f"the split of affinity_text_raw {expected_tokens} "
                    f"(docs/DECISIONS.md §B)"
                )

    # --- Private-data boundary --------------------------------------------
    for name in EXPECTED_FILES:
        text = (sample_dir / name).read_text(encoding="utf-8-sig")
        if PRIVATE_PATH_MARKER in text:
            errors.append(
                f"{name}: contains the private path marker '{PRIVATE_PATH_MARKER}'"
            )

    return ValidationResult(errors=errors, counts=counts)
