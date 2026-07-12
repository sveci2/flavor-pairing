"""Duplicate and reverse-pair detection — report-only (CP6;
docs/DATA_FOUNDATION_PLAN.md §8).

Three **read-only** reports over ``pairing_observations``. Nothing here
mutates, merges, or deletes anything: repeated observations are expected
evidence (one row per source observation, docs/SCHEMA.md §7), and reverse
pairs may legitimately carry different strength evidence per direction —
symmetric aggregation belongs to the out-of-scope derived ``pairing_edges``
layer.

Report definitions (approved CP6 design):

- **Exact duplicate observations** — observations sharing the same
  ``(subject_entity_id, paired_entity_id)`` with *both* entities resolved
  (NULL is never treated as equal to NULL), group size > 1, within or
  across sources.
- **Reverse-pair candidates** — one row per canonical *unordered* entity
  pair where both directions are observed and the entities differ
  (self-pairs excluded). Both directional observation sets are carried
  intact — no score merging, no symmetry inference, no canonical
  direction.
- **Raw-text duplicates** — observations sharing the same
  ``(subject_entity_id, paired_text_raw)`` under **exact** text equality
  (no case-folding or trimming — that would be interpretation), group
  size > 1. This surfaces repeated raw pairings whose ``paired_entity_id``
  may still be unresolved; rows with a NULL ``paired_text_raw`` are not
  groupable and are excluded.

Grouping happens in Python over one ``SELECT`` (no SQL self-joins, so no
join-induced duplicate report rows). Every report row retains full
provenance per observation: ``observation_id``, ``source_id``,
``source_record_id``, raw paired text, entity IDs, strength fields, and
``review_status``. Ordering is deterministic: observations sort by
``observation_id``; groups sort by their group key. Nothing is hard-coded
to any source ID, entity name, or row count.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "DuplicateObservationGroup",
    "ObservationRef",
    "RawTextDuplicateGroup",
    "ReversePairCandidate",
    "duplicate_observation_report",
    "raw_text_duplicate_report",
    "reverse_pair_report",
]


@dataclass(frozen=True)
class ObservationRef:
    """Full provenance for one observation appearing in a report."""

    observation_id: str
    source_id: str
    source_record_id: str
    subject_entity_id: str
    paired_entity_id: Optional[str]
    paired_text_raw: Optional[str]
    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: Optional[str]
    review_status: Optional[str]


@dataclass(frozen=True)
class DuplicateObservationGroup:
    """Several observations asserting the same resolved entity pair."""

    subject_entity_id: str
    paired_entity_id: str
    observations: Tuple[ObservationRef, ...]


@dataclass(frozen=True)
class ReversePairCandidate:
    """Both directions observed for one unordered entity pair.

    ``entity_a`` < ``entity_b`` (canonical ordering of the unordered pair);
    each directional tuple preserves its own observations unmerged.
    """

    entity_a: str
    entity_b: str
    observations_a_to_b: Tuple[ObservationRef, ...]
    observations_b_to_a: Tuple[ObservationRef, ...]


@dataclass(frozen=True)
class RawTextDuplicateGroup:
    """Several observations repeating the same exact raw paired text."""

    subject_entity_id: str
    paired_text_raw: str
    observations: Tuple[ObservationRef, ...]


def _load_observations(connection: sqlite3.Connection) -> List[ObservationRef]:
    rows = connection.execute(
        "SELECT observation_id, source_id, source_record_id, subject_entity_id, "
        "paired_entity_id, paired_text_raw, strength_label, strength_score, "
        "strength_method, review_status FROM pairing_observations "
        "ORDER BY observation_id"
    ).fetchall()
    return [
        ObservationRef(
            observation_id=row["observation_id"],
            source_id=row["source_id"],
            source_record_id=row["source_record_id"],
            subject_entity_id=row["subject_entity_id"],
            paired_entity_id=row["paired_entity_id"],
            paired_text_raw=row["paired_text_raw"],
            strength_label=row["strength_label"],
            strength_score=row["strength_score"],
            strength_method=row["strength_method"],
            review_status=row["review_status"],
        )
        for row in rows
    ]


def duplicate_observation_report(
    connection: sqlite3.Connection,
) -> List[DuplicateObservationGroup]:
    """Groups of observations repeating one fully resolved entity pair."""
    groups: Dict[Tuple[str, str], List[ObservationRef]] = {}
    for observation in _load_observations(connection):
        if observation.paired_entity_id is None:
            continue  # NULL never equals NULL: unresolved rows are not duplicates
        key = (observation.subject_entity_id, observation.paired_entity_id)
        groups.setdefault(key, []).append(observation)
    return [
        DuplicateObservationGroup(
            subject_entity_id=key[0],
            paired_entity_id=key[1],
            observations=tuple(members),
        )
        for key, members in sorted(groups.items())
        if len(members) > 1
    ]


def reverse_pair_report(
    connection: sqlite3.Connection,
) -> List[ReversePairCandidate]:
    """One row per unordered entity pair observed in both directions."""
    directed: Dict[Tuple[str, str], List[ObservationRef]] = {}
    for observation in _load_observations(connection):
        if observation.paired_entity_id is None:
            continue  # unresolved rows carry no direction between entities
        key = (observation.subject_entity_id, observation.paired_entity_id)
        directed.setdefault(key, []).append(observation)

    candidates: List[ReversePairCandidate] = []
    for entity_a, entity_b in sorted(directed):
        if entity_a >= entity_b:
            continue  # visit each unordered pair once; skips self-pairs too
        reverse_key = (entity_b, entity_a)
        if reverse_key not in directed:
            continue  # one direction alone is not a reverse pair
        candidates.append(
            ReversePairCandidate(
                entity_a=entity_a,
                entity_b=entity_b,
                observations_a_to_b=tuple(directed[(entity_a, entity_b)]),
                observations_b_to_a=tuple(directed[reverse_key]),
            )
        )
    return candidates


def raw_text_duplicate_report(
    connection: sqlite3.Connection,
) -> List[RawTextDuplicateGroup]:
    """Groups repeating the same exact raw paired text under one subject."""
    groups: Dict[Tuple[str, str], List[ObservationRef]] = {}
    for observation in _load_observations(connection):
        if observation.paired_text_raw is None:
            continue  # no raw text to repeat
        key = (observation.subject_entity_id, observation.paired_text_raw)
        groups.setdefault(key, []).append(observation)
    return [
        RawTextDuplicateGroup(
            subject_entity_id=key[0],
            paired_text_raw=key[1],
            observations=tuple(members),
        )
        for key, members in sorted(groups.items())
        if len(members) > 1
    ]
