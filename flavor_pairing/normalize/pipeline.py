"""Normalization orchestrator for one source's current version (CP5;
docs/DATA_FOUNDATION_PLAN.md §6, §12, §13).

``normalize_source``:

1. Validates the source is registered and has a completed run, and that
   ``parsed_source_rows`` holds exactly the current version's membership
   (raising ``NormalizeError`` if parsing is missing or stale).
2. Resolves every distinct subject / pairing-entry surface string through
   :func:`flavor_pairing.normalize.entities.resolve_source_names` —
   decision-table writes under the merge rule; **no automatic entity
   creation of any kind**.
3. Rebuilds the source's derived rows — ``entity_attributes`` and
   ``pairing_observations`` — via the pure builders, deleting and
   reinserting them for this source only.

All writes (mapping inserts/updates plus the derived rebuild) share one
transaction: a single commit at the end, full rollback on any error.

This module never writes ``entities``, ``parsed_source_rows``,
``raw_source_rows``, ``import_runs``, or ``run_rows``, and never deletes
any decision-table row. Affinity groups/members, duplicate detection, and
review tooling beyond the queue report remain out of scope
(docs/DATA_FOUNDATION_PLAN.md §20-21).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Tuple

from flavor_pairing.config.loaders import ProjectConfig
from flavor_pairing.ingest.runs import current_version
from flavor_pairing.normalize.attributes import (
    AttributeSource,
    build_entity_attribute_rows,
)
from flavor_pairing.normalize.entities import (
    ROLE_PAIRING_ENTRY,
    ROLE_SUBJECT,
    NameRequest,
    NormalizeError,
    resolve_source_names,
)
from flavor_pairing.normalize.observations import (
    ObservationSource,
    build_pairing_observation_rows,
)
from flavor_pairing.parse.row_parser import (
    ROW_TYPE_ATTRIBUTE,
    ROW_TYPE_PAIRING_CANDIDATE,
)

__all__ = ["NormalizeOutcome", "normalize_source"]


@dataclass(frozen=True)
class NormalizeOutcome:
    """What one ``normalize_source`` call did."""

    source_id: str
    run_id: str
    mappings_created: int
    mappings_updated: int
    unresolved_mappings: int
    attributes_written: int
    observations_written: int
    observations_skipped_unresolved_subject: int


def _load_parsed_rows(
    connection: sqlite3.Connection, source_id: str
) -> List[sqlite3.Row]:
    return connection.execute(
        "SELECT p.source_id, p.source_record_id, p.row_type, p.subject_clean, "
        "p.entry_clean, p.attribute_name, p.attribute_value_raw, p.strength_label, "
        "p.strength_score, p.strength_method, r.subject_raw, r.entry_raw "
        "FROM parsed_source_rows p JOIN raw_source_rows r "
        "ON p.source_id = r.source_id AND p.source_record_id = r.source_record_id "
        "WHERE p.source_id = ? ORDER BY p.source_record_id",
        (source_id,),
    ).fetchall()


def normalize_source(
    connection: sqlite3.Connection, config: ProjectConfig, source_id: str
) -> NormalizeOutcome:
    """Normalize ``source_id``'s current version under the merge rule."""
    config.source(source_id)  # fail fast on unregistered sources

    version = current_version(connection, source_id)
    if version is None:
        raise NormalizeError(
            f"source '{source_id}' has no completed import run; "
            f"ingest and parse it before normalizing"
        )
    parsed = _load_parsed_rows(connection, source_id)
    if not parsed:
        raise NormalizeError(
            f"source '{source_id}' has no parsed rows; run the parser before normalizing"
        )
    parsed_keys = {row["source_record_id"] for row in parsed}
    member_keys = {source_record_id for source_record_id, _ in version.members}
    if parsed_keys != member_keys:
        raise NormalizeError(
            f"parsed rows for source '{source_id}' do not match its current "
            f"version (run '{version.run_id}'); re-run the parser before normalizing"
        )

    requests: Dict[Tuple[str, str], NameRequest] = {}
    attribute_sources: List[AttributeSource] = []
    observation_sources: List[ObservationSource] = []
    for row in parsed:
        if row["row_type"] == ROW_TYPE_ATTRIBUTE:
            requests.setdefault(
                (row["subject_raw"], ROLE_SUBJECT),
                NameRequest(row["subject_raw"], ROLE_SUBJECT, row["subject_clean"]),
            )
            attribute_sources.append(
                AttributeSource(
                    source_id=row["source_id"],
                    source_record_id=row["source_record_id"],
                    subject_raw=row["subject_raw"],
                    attribute_name=row["attribute_name"],
                    attribute_value_raw=row["attribute_value_raw"],
                )
            )
        elif row["row_type"] == ROW_TYPE_PAIRING_CANDIDATE:
            requests.setdefault(
                (row["subject_raw"], ROLE_SUBJECT),
                NameRequest(row["subject_raw"], ROLE_SUBJECT, row["subject_clean"]),
            )
            requests.setdefault(
                (row["entry_raw"], ROLE_PAIRING_ENTRY),
                NameRequest(row["entry_raw"], ROLE_PAIRING_ENTRY, row["entry_clean"]),
            )
            observation_sources.append(
                ObservationSource(
                    source_id=row["source_id"],
                    source_record_id=row["source_record_id"],
                    subject_raw=row["subject_raw"],
                    entry_raw=row["entry_raw"],
                    strength_label=row["strength_label"],
                    strength_score=row["strength_score"],
                    strength_method=row["strength_method"],
                )
            )
        # affinity_header/affinity_group/unclassified rows contribute nothing
        # to CP5 normalization (affinities are CP6; unclassified is review-only).

    try:
        resolutions, counts = resolve_source_names(
            connection, source_id, requests.values()
        )
        attribute_rows = build_entity_attribute_rows(attribute_sources, resolutions)
        observation_rows, skipped = build_pairing_observation_rows(
            observation_sources, resolutions
        )

        connection.execute(
            "DELETE FROM entity_attributes WHERE source_id = ?", (source_id,)
        )
        connection.execute(
            "DELETE FROM pairing_observations WHERE source_id = ?", (source_id,)
        )
        for attribute in attribute_rows:
            connection.execute(
                "INSERT INTO entity_attributes (attribute_id, source_id, "
                "source_record_id, entity_id, attribute_name, attribute_value_raw, "
                "attribute_value_normalized, normalization_method, review_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attribute.attribute_id,
                    attribute.source_id,
                    attribute.source_record_id,
                    attribute.entity_id,
                    attribute.attribute_name,
                    attribute.attribute_value_raw,
                    attribute.attribute_value_normalized,
                    attribute.normalization_method,
                    attribute.review_status,
                ),
            )
        for observation in observation_rows:
            connection.execute(
                "INSERT INTO pairing_observations (observation_id, source_id, "
                "source_record_id, subject_entity_id, paired_entity_id, "
                "paired_text_raw, strength_label, strength_score, strength_method, "
                "normalization_status, review_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation.observation_id,
                    observation.source_id,
                    observation.source_record_id,
                    observation.subject_entity_id,
                    observation.paired_entity_id,
                    observation.paired_text_raw,
                    observation.strength_label,
                    observation.strength_score,
                    observation.strength_method,
                    observation.normalization_status,
                    observation.review_status,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return NormalizeOutcome(
        source_id=source_id,
        run_id=version.run_id,
        mappings_created=counts.mappings_created,
        mappings_updated=counts.mappings_updated,
        unresolved_mappings=counts.unresolved,
        attributes_written=len(attribute_rows),
        observations_written=len(observation_rows),
        observations_skipped_unresolved_subject=skipped,
    )
