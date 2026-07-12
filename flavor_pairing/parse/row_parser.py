"""Stateful row parser over a source's current version (CP4;
docs/DATA_FOUNDATION_PLAN.md §5; docs/SCHEMA.md §3).

Two layers:

- :func:`classify_rows` — pure classification. Takes the current version's
  rows in order plus the format's parser configuration and returns one
  :class:`ParsedRow` per input row. No database access.
- :func:`parse_source` — orchestration. Resolves the source's latest
  completed run (its *current version*, docs/DECISIONS.md §H), reads those
  raw rows, classifies them, and rebuilds ``parsed_source_rows`` for the
  source in one transaction. ``parsed_source_rows`` is a derived table,
  rebuilt on every run (docs/DATA_FOUNDATION_PLAN.md §13); raw rows are
  only ever read here, never written.

Classifier chain (approved CP4 design), first claim wins, applied per row in
``run_rows.source_order``:

1. Blank entry → ``unclassified`` (low confidence, requires review).
2. Label-shaped entry (non-empty text before the first colon): a label
   registered in ``attribute_labels.csv`` for this format (matched
   case-insensitively) → ``attribute``; an unregistered label is **not**
   guessed to be an attribute or a pairing — it becomes ``unclassified``
   for review (docs/SCHEMA.md §10). The attribute value is everything after
   the first colon, outer whitespace stripped, never split further (no
   comma/colon/``e.g.``/``esp.`` splitting — AGENTS.md).
3. Affinity header: with an **approved** ``affinity_split_rules`` row, an
   entry equal (trimmed, case-insensitive) to the header phrase →
   ``affinity_header``; sets the per-block state flag.
4. Affinity group: with an approved rule, an entry containing the registered
   member delimiter → ``affinity_group`` — high confidence after a header in
   the same subject block, otherwise medium confidence and flagged for
   review. Members are *not* split here (that is CP6 scope).
   With a rule that exists but is **not approved**, rows matching its header
   phrase or delimiter become ``unclassified`` + review — an unapproved rule
   never drives structure. With no rule registered, these detectors are
   inactive and rows fall through.
5. Fallback → ``pairing_candidate`` (medium confidence), with typography
   detection and strength resolution applied. An unmapped marker or
   ambiguous typography yields no score and ``requires_review = 1``.

``note`` is a legal ``row_type`` in the schema but this parser never emits
it: no deterministic rule or configuration for recognizing notes exists yet,
and inventing one would violate the no-guessing rules. Recognizing notes
later requires an explicit reviewed rule/config (approved CP4 decision).

Subject-block state: one flag (*affinity header seen*), reset at every
subject boundary. Blocks are maximal runs of consecutive rows with
byte-identical ``subject_raw`` (no trimming or case-folding); non-consecutive
re-appearances of the same subject text are separate blocks, so nothing is
ever inferred across unrelated subjects.

Known edge case (documented, deliberately not special-cased): an affinity
header phrase that itself contains a colon would be claimed by the
label-shape rule first. No registered or plausible configuration does this.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from flavor_pairing.config.loaders import (
    AffinitySplitRule,
    AttributeLabel,
    ProjectConfig,
    StrengthMapping,
)
from flavor_pairing.ingest.runs import current_version
from flavor_pairing.parse.strength import (
    STRENGTH_METHOD_UNAVAILABLE,
    resolve_strength,
)
from flavor_pairing.parse.typography import detect_marker

__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "PARSER_CONFIDENCES",
    "ROW_TYPES",
    "ROW_TYPE_AFFINITY_GROUP",
    "ROW_TYPE_AFFINITY_HEADER",
    "ROW_TYPE_ATTRIBUTE",
    "ROW_TYPE_NOTE",
    "ROW_TYPE_PAIRING_CANDIDATE",
    "ROW_TYPE_UNCLASSIFIED",
    "ParseError",
    "ParseInputRow",
    "ParseOutcome",
    "ParsedRow",
    "classify_rows",
    "parse_source",
]

ROW_TYPE_ATTRIBUTE = "attribute"
ROW_TYPE_AFFINITY_HEADER = "affinity_header"
ROW_TYPE_AFFINITY_GROUP = "affinity_group"
ROW_TYPE_PAIRING_CANDIDATE = "pairing_candidate"
ROW_TYPE_NOTE = "note"  # legal in the schema; never emitted in CP4 (see module docs)
ROW_TYPE_UNCLASSIFIED = "unclassified"
ROW_TYPES = frozenset(
    {
        ROW_TYPE_ATTRIBUTE,
        ROW_TYPE_AFFINITY_HEADER,
        ROW_TYPE_AFFINITY_GROUP,
        ROW_TYPE_PAIRING_CANDIDATE,
        ROW_TYPE_NOTE,
        ROW_TYPE_UNCLASSIFIED,
    }
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
PARSER_CONFIDENCES = frozenset({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW})

APPROVED = "approved"


class ParseError(Exception):
    """A source cannot be parsed (no completed run, or integrity failure)."""


@dataclass(frozen=True)
class ParseInputRow:
    """One current-version raw row, in run order."""

    source_record_id: str
    subject_raw: str
    entry_raw: str
    quality_raw: Optional[str] = None


@dataclass(frozen=True)
class ParsedRow:
    """One ``parsed_source_rows`` row (docs/SCHEMA.md §3)."""

    source_id: str
    source_record_id: str
    row_type: str
    subject_clean: str
    entry_clean: str
    attribute_name: Optional[str]
    attribute_value_raw: Optional[str]
    strength_marker_raw: Optional[str]
    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: str
    parser_confidence: str
    requires_review: int


@dataclass(frozen=True)
class ParseOutcome:
    """What one :func:`parse_source` call produced."""

    source_id: str
    run_id: str
    row_count: int
    row_type_counts: Mapping[str, int]
    requires_review_count: int


def _no_strength() -> Dict[str, object]:
    """Strength fields for row types that carry no strength evidence."""
    return {
        "strength_marker_raw": None,
        "strength_label": None,
        "strength_score": None,
        "strength_method": STRENGTH_METHOD_UNAVAILABLE,
    }


def classify_rows(
    source_id: str,
    rows: Sequence[ParseInputRow],
    *,
    attribute_labels: Mapping[str, AttributeLabel],
    affinity_rule: Optional[AffinitySplitRule],
    strength_mappings: Mapping[str, StrengthMapping],
) -> List[ParsedRow]:
    """Classify one current version's ordered rows into ``ParsedRow``s (1:1)."""
    rule_approved = affinity_rule is not None and affinity_rule.review_status == APPROVED
    header_phrase = (
        affinity_rule.affinity_header_phrase.strip().lower() if affinity_rule else None
    )
    member_delimiter = affinity_rule.member_delimiter if affinity_rule else None

    parsed: List[ParsedRow] = []
    previous_subject: Optional[str] = None
    first_row = True
    header_seen = False

    for row in rows:
        # Subject-block boundary: byte-identical subject_raw comparison.
        if first_row or row.subject_raw != previous_subject:
            header_seen = False
        previous_subject = row.subject_raw
        first_row = False

        subject_clean = row.subject_raw.strip().lower()
        stripped_entry = row.entry_raw.strip()

        matches_header = (
            header_phrase is not None and stripped_entry.lower() == header_phrase
        )
        matches_delimiter = (
            member_delimiter is not None and member_delimiter in row.entry_raw
        )

        common = {"source_id": source_id, "source_record_id": row.source_record_id}

        # 1. Blank entry.
        if not stripped_entry:
            parsed.append(
                ParsedRow(
                    row_type=ROW_TYPE_UNCLASSIFIED,
                    subject_clean=subject_clean,
                    entry_clean=stripped_entry,
                    attribute_name=None,
                    attribute_value_raw=None,
                    parser_confidence=CONFIDENCE_LOW,
                    requires_review=1,
                    **common,
                    **_no_strength(),
                )
            )
            continue

        # 2. Label-shaped entry: non-empty text before the first colon.
        if ":" in row.entry_raw:
            label_text, _, value_text = row.entry_raw.partition(":")
            label_key = label_text.strip().lower()
            if label_key:
                registered = attribute_labels.get(label_key)
                if registered is not None:
                    parsed.append(
                        ParsedRow(
                            row_type=ROW_TYPE_ATTRIBUTE,
                            subject_clean=subject_clean,
                            entry_clean=stripped_entry,
                            attribute_name=registered.attribute_name,
                            attribute_value_raw=value_text.strip(),
                            parser_confidence=CONFIDENCE_HIGH,
                            requires_review=0,
                            **common,
                            **_no_strength(),
                        )
                    )
                else:
                    # Unregistered label: never guessed to be an attribute
                    # or a pairing (docs/SCHEMA.md §10).
                    parsed.append(
                        ParsedRow(
                            row_type=ROW_TYPE_UNCLASSIFIED,
                            subject_clean=subject_clean,
                            entry_clean=stripped_entry,
                            attribute_name=None,
                            attribute_value_raw=None,
                            parser_confidence=CONFIDENCE_LOW,
                            requires_review=1,
                            **common,
                            **_no_strength(),
                        )
                    )
                continue

        # 3./4. Affinity header and group — approved rules only.
        if rule_approved and matches_header:
            header_seen = True
            parsed.append(
                ParsedRow(
                    row_type=ROW_TYPE_AFFINITY_HEADER,
                    subject_clean=subject_clean,
                    entry_clean=stripped_entry.lower(),
                    attribute_name=None,
                    attribute_value_raw=None,
                    parser_confidence=CONFIDENCE_HIGH,
                    requires_review=0,
                    **common,
                    **_no_strength(),
                )
            )
            continue
        if rule_approved and matches_delimiter:
            parsed.append(
                ParsedRow(
                    row_type=ROW_TYPE_AFFINITY_GROUP,
                    subject_clean=subject_clean,
                    entry_clean=stripped_entry.lower(),
                    attribute_name=None,
                    attribute_value_raw=None,
                    parser_confidence=CONFIDENCE_HIGH if header_seen else CONFIDENCE_MEDIUM,
                    requires_review=0 if header_seen else 1,
                    **common,
                    **_no_strength(),
                )
            )
            continue
        if not rule_approved and (matches_header or matches_delimiter):
            # A rule exists but is not approved: it must not drive structure,
            # and its matches must stay visible for review, not be guessed.
            parsed.append(
                ParsedRow(
                    row_type=ROW_TYPE_UNCLASSIFIED,
                    subject_clean=subject_clean,
                    entry_clean=stripped_entry,
                    attribute_name=None,
                    attribute_value_raw=None,
                    parser_confidence=CONFIDENCE_LOW,
                    requires_review=1,
                    **common,
                    **_no_strength(),
                )
            )
            continue

        # 5. Pairing-candidate fallback, with strength resolution.
        marker = detect_marker(row.entry_raw, row.quality_raw)
        resolution = resolve_strength(strength_mappings, marker.marker_key)
        parsed.append(
            ParsedRow(
                row_type=ROW_TYPE_PAIRING_CANDIDATE,
                subject_clean=subject_clean,
                entry_clean=stripped_entry.lstrip("*").strip().lower(),
                attribute_name=None,
                attribute_value_raw=None,
                strength_marker_raw=marker.strength_marker_raw,
                strength_label=resolution.strength_label,
                strength_score=resolution.strength_score,
                strength_method=resolution.strength_method,
                parser_confidence=CONFIDENCE_MEDIUM,
                requires_review=1 if (not resolution.mapped or marker.ambiguous) else 0,
                **common,
            )
        )

    return parsed


def parse_source(
    connection: sqlite3.Connection, config: ProjectConfig, source_id: str
) -> ParseOutcome:
    """Parse ``source_id``'s current version into ``parsed_source_rows``.

    - Operates only on the latest *completed* run's membership
      (docs/DECISIONS.md §H): raw rows absent from it — removed or edited
      away in a later version — get no parsed row.
    - Fails fast (``ConfigError``) if the source is unregistered or its
      format has no strength mappings; fails (``ParseError``) if the source
      has no completed run.
    - Rebuilds the source's ``parsed_source_rows`` atomically: within one
      transaction, deletes the source's existing parsed rows and inserts
      exactly one row per current-version raw row. Rerunning on the same
      version reproduces identical output. ``raw_source_rows`` is only ever
      read.
    """
    source = config.source(source_id)
    strength_mappings = config.strength_mappings_for(source.source_format)
    attribute_labels = config.attribute_labels_for(source.source_format)
    affinity_rule = config.affinity_rule_for(source.source_format)

    version = current_version(connection, source_id)
    if version is None:
        raise ParseError(
            f"source '{source_id}' has no completed import run; "
            f"ingest it before parsing"
        )

    raw_by_id = {
        raw["source_record_id"]: raw
        for raw in connection.execute(
            "SELECT source_record_id, subject_raw, entry_raw, quality_raw "
            "FROM raw_source_rows WHERE source_id = ?",
            (source_id,),
        )
    }
    ordered_rows: List[ParseInputRow] = []
    for source_record_id, _source_order in version.members:
        raw = raw_by_id.get(source_record_id)
        if raw is None:
            raise ParseError(
                f"run '{version.run_id}' references source_record_id "
                f"'{source_record_id}' which is missing from raw_source_rows; "
                f"the ledger and raw table are inconsistent"
            )
        ordered_rows.append(
            ParseInputRow(
                source_record_id=source_record_id,
                subject_raw=raw["subject_raw"],
                entry_raw=raw["entry_raw"],
                quality_raw=raw["quality_raw"],
            )
        )

    parsed = classify_rows(
        source_id,
        ordered_rows,
        attribute_labels=attribute_labels,
        affinity_rule=affinity_rule,
        strength_mappings=strength_mappings,
    )

    try:
        connection.execute(
            "DELETE FROM parsed_source_rows WHERE source_id = ?", (source_id,)
        )
        for row in parsed:
            connection.execute(
                "INSERT INTO parsed_source_rows "
                "(source_id, source_record_id, row_type, subject_clean, entry_clean, "
                "attribute_name, attribute_value_raw, strength_marker_raw, "
                "strength_label, strength_score, strength_method, parser_confidence, "
                "requires_review) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.source_id,
                    row.source_record_id,
                    row.row_type,
                    row.subject_clean,
                    row.entry_clean,
                    row.attribute_name,
                    row.attribute_value_raw,
                    row.strength_marker_raw,
                    row.strength_label,
                    row.strength_score,
                    row.strength_method,
                    row.parser_confidence,
                    row.requires_review,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    row_type_counts = dict(Counter(row.row_type for row in parsed))
    return ParseOutcome(
        source_id=source_id,
        run_id=version.run_id,
        row_count=len(parsed),
        row_type_counts=row_type_counts,
        requires_review_count=sum(row.requires_review for row in parsed),
    )
