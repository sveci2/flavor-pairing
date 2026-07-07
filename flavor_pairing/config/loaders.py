"""Loaders and validation for the five configuration (decision) tables.

Covers ``sources.csv``, ``import_mappings.csv``, ``strength_mappings.csv``,
``attribute_labels.csv``, and ``affinity_split_rules.csv`` — see
``docs/SCHEMA.md`` §10 and ``docs/DECISIONS.md`` §E/§I/§J.

Design rules honoured here (see ``CLAUDE.md``):

- Standard library only; no network access; no path assumptions beyond the
  configuration directory passed in by the caller.
- Nothing is hard-coded to particular source IDs, source formats, or row
  counts: the configuration files are the source of truth.
- Loading the full configuration does not assume every source format needs
  attribute labels or an affinity split rule; the ``require_*`` accessors
  fail with clear messages when a capability is requested but missing or
  unapproved.
- This module intentionally contains no parsing, ingestion, storage,
  normalization, or review logic (CP1 scope).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = [
    "AffinitySplitRule",
    "AttributeLabel",
    "ColumnMapping",
    "ConfigError",
    "ProjectConfig",
    "Source",
    "StrengthMapping",
    "load_affinity_split_rules",
    "load_attribute_labels",
    "load_config",
    "load_import_mappings",
    "load_sources",
    "load_strength_mappings",
]

# Closed marker-key set (docs/SCHEMA.md §10; docs/DATA_FOUNDATION_PLAN.md §11).
FIXED_MARKER_KEYS = frozenset({"plain", "uppercase", "asterisk_uppercase"})
EXPLICIT_LABEL_PREFIX = "explicit_label:"

# Raw-layer fields a flat tabular mapping may feed (docs/SCHEMA.md §2 and §10).
RAW_TARGET_FIELDS = ("subject_raw", "entry_raw", "quality_raw")
REQUIRED_TARGET_FIELDS = ("subject_raw", "entry_raw")
KNOWN_TARGET_FILES = frozenset({"raw_source_rows.csv"})

# Placeholder used in import_mappings.csv when the source has no such column.
NOT_PRESENT = "(not present)"

VALID_SCORES = frozenset({1, 2, 3, 4})
VALID_REVIEW_STATUSES = frozenset({"approved", "needs_review", "rejected"})

SOURCES_COLUMNS = (
    "source_id",
    "source_name",
    "source_format",
    "source_uri",
    "rights_status",
    "allowed_use",
    "notes",
)
IMPORT_MAPPINGS_COLUMNS = (
    "source_format",
    "input_column",
    "target_file",
    "target_field",
    "transform_rule",
    "required",
)
STRENGTH_MAPPINGS_COLUMNS = (
    "input_source_format",
    "marker_key",
    "source_value_or_marker",
    "normalized_label",
    "normalized_score",
    "mapping_confidence",
    "notes",
)
ATTRIBUTE_LABELS_COLUMNS = (
    "source_format",
    "source_label",
    "attribute_name",
    "notes",
)
AFFINITY_SPLIT_RULES_COLUMNS = (
    "source_format",
    "affinity_header_phrase",
    "member_delimiter",
    "review_status",
    "notes",
)


class ConfigError(ValueError):
    """A configuration file is missing, malformed, or inconsistent.

    Messages always name the file (and line where applicable) plus what to fix.
    """


@dataclass(frozen=True)
class Source:
    source_id: str
    source_name: str
    source_format: str
    source_uri: str
    rights_status: str
    allowed_use: str
    notes: str


@dataclass(frozen=True)
class ColumnMapping:
    source_format: str
    target_field: str
    input_column: Optional[str]  # None when the source has no such column
    required: bool
    transform_rule: str


@dataclass(frozen=True)
class StrengthMapping:
    input_source_format: str
    marker_key: str
    source_value_or_marker: str
    normalized_label: Optional[str]
    normalized_score: Optional[int]
    mapping_confidence: str
    notes: str


@dataclass(frozen=True)
class AttributeLabel:
    source_format: str
    source_label: str
    attribute_name: str
    notes: str


@dataclass(frozen=True)
class AffinitySplitRule:
    source_format: str
    affinity_header_phrase: str
    member_delimiter: str
    review_status: str
    notes: str


def _read_rows(path: Path, expected_columns: Tuple[str, ...]) -> List[Tuple[int, Dict[str, str]]]:
    """Read a config CSV BOM-safely, enforcing the expected header.

    Returns ``(line_number, row)`` pairs; line numbers are 1-based file lines
    (the header is line 1) so error messages point at the offending line.
    """
    if not path.is_file():
        raise ConfigError(
            f"{path}: configuration file not found; "
            f"create it from data/templates/{path.name}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [column for column in expected_columns if column not in header]
        if missing:
            raise ConfigError(
                f"{path.name}: missing required column(s) {missing}; "
                f"expected header {list(expected_columns)}, found {list(header)}"
            )
        rows: List[Tuple[int, Dict[str, str]]] = []
        for line_number, row in enumerate(reader, start=2):
            if row.get(None):
                raise ConfigError(
                    f"{path.name} line {line_number}: more values than header columns; "
                    f"check for unquoted commas"
                )
            rows.append((line_number, {key: (value or "") for key, value in row.items() if key is not None}))
    return rows


def _require_fields(
    path: Path, line_number: int, row: Dict[str, str], fields: Tuple[str, ...]
) -> None:
    for name in fields:
        if not row[name]:
            raise ConfigError(
                f"{path.name} line {line_number}: '{name}' must not be blank"
            )


def load_sources(path: Path) -> Dict[str, Source]:
    """Load sources.csv keyed by source_id."""
    sources: Dict[str, Source] = {}
    for line_number, row in _read_rows(path, SOURCES_COLUMNS):
        _require_fields(
            path, line_number, row,
            ("source_id", "source_name", "source_format", "rights_status"),
        )
        source_id = row["source_id"]
        if source_id in sources:
            raise ConfigError(
                f"{path.name} line {line_number}: duplicate source_id '{source_id}'; "
                f"each source must be registered exactly once"
            )
        sources[source_id] = Source(
            source_id=source_id,
            source_name=row["source_name"],
            source_format=row["source_format"],
            source_uri=row["source_uri"],
            rights_status=row["rights_status"],
            allowed_use=row["allowed_use"],
            notes=row["notes"],
        )
    return sources


def load_import_mappings(path: Path) -> Dict[str, Dict[str, ColumnMapping]]:
    """Load import_mappings.csv as {source_format: {target_field: ColumnMapping}}.

    Enforces per-format completeness for flat tabular sources: every format
    must map all raw target fields, and the required fields (subject_raw,
    entry_raw) must map to a real input column.
    """
    mappings: Dict[str, Dict[str, ColumnMapping]] = {}
    for line_number, row in _read_rows(path, IMPORT_MAPPINGS_COLUMNS):
        _require_fields(path, line_number, row, ("source_format", "target_file", "target_field"))
        source_format = row["source_format"]
        target_field = row["target_field"]
        if target_field not in RAW_TARGET_FIELDS:
            raise ConfigError(
                f"{path.name} line {line_number}: unknown target_field '{target_field}'; "
                f"expected one of {list(RAW_TARGET_FIELDS)}"
            )
        if row["target_file"] not in KNOWN_TARGET_FILES:
            raise ConfigError(
                f"{path.name} line {line_number}: unknown target_file '{row['target_file']}'; "
                f"expected one of {sorted(KNOWN_TARGET_FILES)}"
            )
        if row["required"] not in ("0", "1"):
            raise ConfigError(
                f"{path.name} line {line_number}: required must be '0' or '1', "
                f"got '{row['required']}'"
            )
        required = row["required"] == "1"
        input_column: Optional[str] = row["input_column"]
        if input_column in ("", NOT_PRESENT):
            input_column = None
        if required and input_column is None:
            raise ConfigError(
                f"{path.name} line {line_number}: target_field '{target_field}' for "
                f"source_format '{source_format}' is required but maps no input column; "
                f"name the input column or mark the mapping as not required"
            )
        per_format = mappings.setdefault(source_format, {})
        if target_field in per_format:
            raise ConfigError(
                f"{path.name} line {line_number}: duplicate mapping for "
                f"(source_format '{source_format}', target_field '{target_field}'); "
                f"each target field may be mapped once per format"
            )
        per_format[target_field] = ColumnMapping(
            source_format=source_format,
            target_field=target_field,
            input_column=input_column,
            required=required,
            transform_rule=row["transform_rule"],
        )
    for source_format, per_format in mappings.items():
        missing = [name for name in RAW_TARGET_FIELDS if name not in per_format]
        if missing:
            raise ConfigError(
                f"{path.name}: incomplete flat-tabular mapping for source_format "
                f"'{source_format}': missing target_field(s) {missing}; declare every "
                f"raw target field, using input_column '{NOT_PRESENT}' where the source "
                f"has no such column"
            )
        for name in REQUIRED_TARGET_FIELDS:
            if per_format[name].input_column is None:
                raise ConfigError(
                    f"{path.name}: source_format '{source_format}' maps no input column "
                    f"to required target_field '{name}'"
                )
    return mappings


def _validate_marker_key(path: Path, line_number: int, marker_key: str) -> None:
    if marker_key in FIXED_MARKER_KEYS:
        return
    if (
        marker_key.startswith(EXPLICIT_LABEL_PREFIX)
        and len(marker_key) > len(EXPLICIT_LABEL_PREFIX)
    ):
        return
    raise ConfigError(
        f"{path.name} line {line_number}: invalid marker_key '{marker_key}'; "
        f"expected one of {sorted(FIXED_MARKER_KEYS)} or "
        f"'{EXPLICIT_LABEL_PREFIX}<value>' with a non-empty value"
    )


def load_strength_mappings(path: Path) -> Dict[str, Dict[str, StrengthMapping]]:
    """Load strength_mappings.csv as {input_source_format: {marker_key: StrengthMapping}}."""
    mappings: Dict[str, Dict[str, StrengthMapping]] = {}
    for line_number, row in _read_rows(path, STRENGTH_MAPPINGS_COLUMNS):
        _require_fields(path, line_number, row, ("input_source_format", "marker_key"))
        source_format = row["input_source_format"]
        marker_key = row["marker_key"]
        _validate_marker_key(path, line_number, marker_key)

        score_text = row["normalized_score"]
        label = row["normalized_label"]
        score: Optional[int] = None
        if score_text:
            try:
                score = int(score_text)
            except ValueError:
                raise ConfigError(
                    f"{path.name} line {line_number}: normalized_score must be an "
                    f"integer 1-4 or blank, got '{score_text}'"
                ) from None
            if score not in VALID_SCORES:
                raise ConfigError(
                    f"{path.name} line {line_number}: normalized_score must be in "
                    f"{sorted(VALID_SCORES)} or blank, got {score}"
                )
        if bool(label) != bool(score_text):
            raise ConfigError(
                f"{path.name} line {line_number}: normalized_label and normalized_score "
                f"must both be present or both be blank (a marker either normalizes to "
                f"a scored label or carries no strength evidence)"
            )

        per_format = mappings.setdefault(source_format, {})
        if marker_key in per_format:
            raise ConfigError(
                f"{path.name} line {line_number}: duplicate marker_key '{marker_key}' "
                f"for input_source_format '{source_format}'; each marker may be mapped "
                f"once per format"
            )
        per_format[marker_key] = StrengthMapping(
            input_source_format=source_format,
            marker_key=marker_key,
            source_value_or_marker=row["source_value_or_marker"],
            normalized_label=label or None,
            normalized_score=score,
            mapping_confidence=row["mapping_confidence"],
            notes=row["notes"],
        )
    return mappings


def load_attribute_labels(path: Path) -> Dict[str, Dict[str, AttributeLabel]]:
    """Load attribute_labels.csv as {source_format: {lowercased label: AttributeLabel}}.

    Labels are matched case-insensitively (docs/SCHEMA.md §10), so keys are
    lowercased and duplicates are detected case-insensitively.
    """
    labels: Dict[str, Dict[str, AttributeLabel]] = {}
    for line_number, row in _read_rows(path, ATTRIBUTE_LABELS_COLUMNS):
        _require_fields(path, line_number, row, ("source_format", "source_label", "attribute_name"))
        source_format = row["source_format"]
        label_key = row["source_label"].lower()
        per_format = labels.setdefault(source_format, {})
        if label_key in per_format:
            raise ConfigError(
                f"{path.name} line {line_number}: duplicate source_label "
                f"'{row['source_label']}' for source_format '{source_format}' "
                f"(labels are matched case-insensitively)"
            )
        per_format[label_key] = AttributeLabel(
            source_format=source_format,
            source_label=row["source_label"],
            attribute_name=row["attribute_name"],
            notes=row["notes"],
        )
    return labels


def load_affinity_split_rules(path: Path) -> Dict[str, AffinitySplitRule]:
    """Load affinity_split_rules.csv as {source_format: AffinitySplitRule}.

    One rule per source format (docs/SCHEMA.md §10). The member_delimiter is
    taken exactly as written, including any surrounding spaces.
    """
    rules: Dict[str, AffinitySplitRule] = {}
    for line_number, row in _read_rows(path, AFFINITY_SPLIT_RULES_COLUMNS):
        _require_fields(
            path, line_number, row,
            ("source_format", "affinity_header_phrase", "member_delimiter", "review_status"),
        )
        source_format = row["source_format"]
        if row["review_status"] not in VALID_REVIEW_STATUSES:
            raise ConfigError(
                f"{path.name} line {line_number}: invalid review_status "
                f"'{row['review_status']}'; expected one of {sorted(VALID_REVIEW_STATUSES)}"
            )
        if source_format in rules:
            raise ConfigError(
                f"{path.name} line {line_number}: duplicate rule for source_format "
                f"'{source_format}'; each format registers exactly one split rule"
            )
        rules[source_format] = AffinitySplitRule(
            source_format=source_format,
            affinity_header_phrase=row["affinity_header_phrase"],
            member_delimiter=row["member_delimiter"],
            review_status=row["review_status"],
            notes=row["notes"],
        )
    return rules


@dataclass(frozen=True)
class ProjectConfig:
    """The five loaded configuration tables plus capability accessors."""

    config_dir: Path
    sources: Dict[str, Source]
    import_mappings: Dict[str, Dict[str, ColumnMapping]]
    strength_mappings: Dict[str, Dict[str, StrengthMapping]]
    attribute_labels: Dict[str, Dict[str, AttributeLabel]] = field(default_factory=dict)
    affinity_split_rules: Dict[str, AffinitySplitRule] = field(default_factory=dict)

    def source(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError:
            raise ConfigError(
                f"unknown source_id '{source_id}'; register it in sources.csv "
                f"(known: {sorted(self.sources)})"
            ) from None

    def mapping_for(self, source_format: str) -> Dict[str, ColumnMapping]:
        try:
            return self.import_mappings[source_format]
        except KeyError:
            raise ConfigError(
                f"no import mappings for source_format '{source_format}'; add its "
                f"column mappings to import_mappings.csv "
                f"(known formats: {sorted(self.import_mappings)})"
            ) from None

    def strength_mappings_for(self, source_format: str) -> Dict[str, StrengthMapping]:
        try:
            return self.strength_mappings[source_format]
        except KeyError:
            raise ConfigError(
                f"no strength mappings for source_format '{source_format}'; declare its "
                f"marker mappings in strength_mappings.csv "
                f"(known formats: {sorted(self.strength_mappings)})"
            ) from None

    def attribute_labels_for(self, source_format: str) -> Dict[str, AttributeLabel]:
        """Attribute labels for a format; empty if none configured (not an error)."""
        return self.attribute_labels.get(source_format, {})

    def require_attribute_labels(self, source_format: str) -> Dict[str, AttributeLabel]:
        labels = self.attribute_labels_for(source_format)
        if not labels:
            raise ConfigError(
                f"no attribute labels configured for source_format '{source_format}'; "
                f"attribute-line parsing for this format requires registered labels in "
                f"attribute_labels.csv"
            )
        return labels

    def affinity_rule_for(self, source_format: str) -> Optional[AffinitySplitRule]:
        """The format's affinity split rule regardless of review status, or None."""
        return self.affinity_split_rules.get(source_format)

    def require_affinity_rule(self, source_format: str) -> AffinitySplitRule:
        rule = self.affinity_rule_for(source_format)
        if rule is None:
            raise ConfigError(
                f"no affinity split rule configured for source_format '{source_format}'; "
                f"affinity parsing for this format requires a reviewed rule in "
                f"affinity_split_rules.csv"
            )
        if rule.review_status != "approved":
            raise ConfigError(
                f"affinity split rule for source_format '{source_format}' has "
                f"review_status '{rule.review_status}'; only approved rules may be used "
                f"for splitting (review and approve it in affinity_split_rules.csv)"
            )
        return rule


def load_config(config_dir: Path) -> ProjectConfig:
    """Load and cross-validate all five configuration tables from one directory.

    Cross-file checks:

    - every source's source_format must have complete import mappings;
    - every source_format referenced by strength_mappings, attribute_labels,
      or affinity_split_rules must be a format known to import_mappings
      (typo protection).

    Formats may be configured before any source uses them; attribute labels
    and affinity rules are only required per format when a capability
    accessor asks for them.
    """
    config_dir = Path(config_dir)
    sources = load_sources(config_dir / "sources.csv")
    import_mappings = load_import_mappings(config_dir / "import_mappings.csv")
    strength_mappings = load_strength_mappings(config_dir / "strength_mappings.csv")
    attribute_labels = load_attribute_labels(config_dir / "attribute_labels.csv")
    affinity_split_rules = load_affinity_split_rules(config_dir / "affinity_split_rules.csv")

    known_formats = set(import_mappings)
    for source in sources.values():
        if source.source_format not in known_formats:
            raise ConfigError(
                f"sources.csv: source '{source.source_id}' uses source_format "
                f"'{source.source_format}', which has no rows in import_mappings.csv; "
                f"register the format's column mappings or correct the source entry "
                f"(known formats: {sorted(known_formats)})"
            )
    for file_name, formats in (
        ("strength_mappings.csv", strength_mappings),
        ("attribute_labels.csv", attribute_labels),
        ("affinity_split_rules.csv", affinity_split_rules),
    ):
        for source_format in formats:
            if source_format not in known_formats:
                raise ConfigError(
                    f"{file_name}: source_format '{source_format}' has no rows in "
                    f"import_mappings.csv; register the format or correct the entry "
                    f"(known formats: {sorted(known_formats)})"
                )

    return ProjectConfig(
        config_dir=config_dir,
        sources=sources,
        import_mappings=import_mappings,
        strength_mappings=strength_mappings,
        attribute_labels=attribute_labels,
        affinity_split_rules=affinity_split_rules,
    )
