"""Derived ``entity_attributes`` rows from parsed attribute rows (CP5;
docs/SCHEMA.md §6; docs/DATA_FOUNDATION_PLAN.md §6, §13).

Pure builder — no database access. Rules (approved CP5 design):

- Only the attribute row's **subject** is resolved (role ``subject``);
  attribute *values* never enter entity resolution and never create
  entities (docs/DECISIONS.md §C).
- A row is written even when the subject entity is unresolved
  (``entity_id`` NULL — docs/SCHEMA.md §6 allows it), preserving the
  observation for review.
- ``attribute_value_raw`` is carried verbatim; ``attribute_value_normalized``
  stays NULL in CP5 (no reviewed normalization rules exist; nothing is
  invented), with ``normalization_method = 'not_normalized'``.
- ``review_status`` is deterministic: ``'approved'`` only when the subject
  resolved through a ``human_mapped`` mapping (the row restates a human
  decision); otherwise ``'needs_review'``. Vocabulary is the documented set
  {needs_review, approved, rejected} (docs/DECISIONS.md §J).
- ``attribute_id`` is content-derived from ``source_record_id`` (which
  already embeds the source), so rebuilds are deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_NEEDS_REVIEW,
    ROLE_SUBJECT,
    NameResolution,
)

__all__ = ["AttributeSource", "EntityAttributeRow", "attribute_id_for",
           "build_entity_attribute_rows"]

NORMALIZATION_METHOD_NOT_NORMALIZED = "not_normalized"


def attribute_id_for(source_record_id: str) -> str:
    """Deterministic content-derived attribute_id for one parsed row."""
    canonical = json.dumps([source_record_id], ensure_ascii=False, separators=(",", ":"))
    return f"attr_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class AttributeSource:
    """One current-version parsed attribute row plus its raw subject text."""

    source_id: str
    source_record_id: str
    subject_raw: str
    attribute_name: Optional[str]
    attribute_value_raw: Optional[str]


@dataclass(frozen=True)
class EntityAttributeRow:
    """One ``entity_attributes`` row (docs/SCHEMA.md §6)."""

    attribute_id: str
    source_id: str
    source_record_id: str
    entity_id: Optional[str]
    attribute_name: Optional[str]
    attribute_value_raw: Optional[str]
    attribute_value_normalized: Optional[str]
    normalization_method: str
    review_status: str


def build_entity_attribute_rows(
    rows: Sequence[AttributeSource],
    resolutions: Dict[Tuple[str, str], NameResolution],
) -> List[EntityAttributeRow]:
    """Build one derived attribute row per parsed attribute row (1:1)."""
    built: List[EntityAttributeRow] = []
    for row in rows:
        resolution = resolutions[(row.subject_raw, ROLE_SUBJECT)]
        approved = (
            resolution.entity_id is not None
            and resolution.normalization_status == NORMALIZATION_STATUS_HUMAN_MAPPED
        )
        built.append(
            EntityAttributeRow(
                attribute_id=attribute_id_for(row.source_record_id),
                source_id=row.source_id,
                source_record_id=row.source_record_id,
                entity_id=resolution.entity_id,
                attribute_name=row.attribute_name,
                attribute_value_raw=row.attribute_value_raw,
                attribute_value_normalized=None,
                normalization_method=NORMALIZATION_METHOD_NOT_NORMALIZED,
                review_status=(
                    REVIEW_STATUS_APPROVED if approved else REVIEW_STATUS_NEEDS_REVIEW
                ),
            )
        )
    return built
