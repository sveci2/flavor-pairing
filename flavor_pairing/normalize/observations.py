"""Derived ``pairing_observations`` rows from parsed pairing candidates
(CP5; docs/SCHEMA.md §7; docs/DATA_FOUNDATION_PLAN.md §6, §8, §13).

Pure builder — no database access. Rules (approved CP5 design):

- ``subject_entity_id`` comes only from the resolved subject mapping and
  ``paired_entity_id`` only from the resolved pairing-entry mapping. No
  other resolution path exists.
- ``subject_entity_id`` is NOT NULL in the schema, so a row whose subject
  is unresolved is **skipped** (returned in the skip count, surfaced via
  the parsed row and unresolved mapping in the review queue) — never
  satisfied with a placeholder entity.
- ``paired_entity_id`` may stay NULL for an unresolved pairing entry
  (docs/SCHEMA.md §7); ``paired_text_raw`` is always the exact raw entry
  text.
- Strength label/score/method are copied byte-for-byte from
  ``parsed_source_rows`` — never reinterpreted, never defaulted.
- ``normalization_status``: the paired mapping's status copied verbatim
  when the paired entity resolved, ``'unresolved'`` when it did not.
- ``review_status`` is deterministic: ``'approved'`` only when *both* the
  subject and the paired entity resolved through ``human_mapped`` mappings
  (the row purely restates human decisions); otherwise ``'needs_review'``.
  Vocabulary is the documented {needs_review, approved, rejected} set
  (docs/DECISIONS.md §J).
- ``observation_id`` is content-derived from ``source_record_id``,
  preserving the 1:1 observation-per-row invariant deterministically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_NEEDS_REVIEW,
    ROLE_PAIRING_ENTRY,
    ROLE_SUBJECT,
    NameResolution,
)

__all__ = ["ObservationSource", "PairingObservationRow", "observation_id_for",
           "build_pairing_observation_rows"]


def observation_id_for(source_record_id: str) -> str:
    """Deterministic content-derived observation_id for one parsed row."""
    canonical = json.dumps([source_record_id], ensure_ascii=False, separators=(",", ":"))
    return f"obs_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class ObservationSource:
    """One current-version parsed pairing_candidate row plus raw texts."""

    source_id: str
    source_record_id: str
    subject_raw: str
    entry_raw: str
    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: Optional[str]


@dataclass(frozen=True)
class PairingObservationRow:
    """One ``pairing_observations`` row (docs/SCHEMA.md §7)."""

    observation_id: str
    source_id: str
    source_record_id: str
    subject_entity_id: str
    paired_entity_id: Optional[str]
    paired_text_raw: str
    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: Optional[str]
    normalization_status: Optional[str]
    review_status: str


def build_pairing_observation_rows(
    rows: Sequence[ObservationSource],
    resolutions: Dict[Tuple[str, str], NameResolution],
) -> Tuple[List[PairingObservationRow], int]:
    """Build observation rows; returns (rows, skipped_unresolved_subject)."""
    built: List[PairingObservationRow] = []
    skipped = 0
    for row in rows:
        subject = resolutions[(row.subject_raw, ROLE_SUBJECT)]
        if subject.entity_id is None:
            skipped += 1
            continue
        paired = resolutions[(row.entry_raw, ROLE_PAIRING_ENTRY)]
        both_human = (
            subject.normalization_status == NORMALIZATION_STATUS_HUMAN_MAPPED
            and paired.entity_id is not None
            and paired.normalization_status == NORMALIZATION_STATUS_HUMAN_MAPPED
        )
        built.append(
            PairingObservationRow(
                observation_id=observation_id_for(row.source_record_id),
                source_id=row.source_id,
                source_record_id=row.source_record_id,
                subject_entity_id=subject.entity_id,
                paired_entity_id=paired.entity_id,
                paired_text_raw=row.entry_raw,
                strength_label=row.strength_label,
                strength_score=row.strength_score,
                strength_method=row.strength_method,
                normalization_status=(
                    paired.normalization_status
                    if paired.entity_id is not None
                    else NORMALIZATION_STATUS_UNRESOLVED
                ),
                review_status=(
                    REVIEW_STATUS_APPROVED if both_human else REVIEW_STATUS_NEEDS_REVIEW
                ),
            )
        )
    return built, skipped
