"""Entity/source-name resolution and decision-table write rules (CP5;
docs/DATA_FOUNDATION_PLAN.md §6, §13; docs/DECISIONS.md §A, §C, §J).

``entities`` and ``entity_source_names`` are durable, human-owned decision
tables. This module is the only normalize-layer code that writes to them,
under these exact rules (approved CP5 design):

Resolution order for one ``(source_id, source_text, source_role)`` key:

1. An existing ``entity_source_names`` row for the exact key. A row whose
   ``normalization_status`` is outside ``MACHINE_MAPPING_STATUSES`` —
   ``human_mapped`` or any unrecognized value — is human-owned: it is used
   as-is (mapped or null) and never modified. A machine-owned row with an
   entity keeps it; a machine-owned row with a null entity is retried
   against step 2 and updated only on success.
2. Exact canonical entity-name match: candidates are entities whose
   ``canonical_name`` equals the parser's clean text after trim + case-fold
   — nothing more aggressive (no plural/fuzzy matching) — excluding
   entities with ``review_status = 'rejected'`` (the machine never maps to,
   or silently recreates, a rejected concept). Exactly one candidate
   resolves; zero or several do not.
3. Otherwise the mapping stays **unresolved**: ``entity_id`` NULL,
   ``normalization_status = 'unresolved'``. No placeholder entity, no new
   entity row — automatic entity creation is not permitted. Composite or
   descriptive phrases therefore remain unresolved for review.

Machine writes are limited to: INSERT of new mapping rows (auto_mapped or
unresolved) and UPDATE of ``entity_id``/``normalization_status`` on
machine-owned null rows. ``source_name_id``, ``source_id``, ``source_text``,
and ``source_role`` are never changed on any existing row; no decision row
is ever deleted; ``entities`` is never automatically updated or deleted.

:func:`create_reviewed_entity` is the only entity-creation path. It records
an explicit reviewed decision supplied by the caller (who provides the
``entity_id``); it is never invoked by ``normalize_source``. Without
supported type evidence the type stays ``'unknown'`` — never inferred as
ingredient, technique, or anything else — and ``display_name`` may stay
NULL rather than inventing cosmetics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "ENTITY_TYPES",
    "ENTITY_TYPE_UNKNOWN",
    "MACHINE_MAPPING_STATUSES",
    "NORMALIZATION_STATUS_AUTO_MAPPED",
    "NORMALIZATION_STATUS_HUMAN_MAPPED",
    "NORMALIZATION_STATUS_UNRESOLVED",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_APPROVED",
    "REVIEW_STATUS_NEEDS_REVIEW",
    "REVIEW_STATUS_REJECTED",
    "ROLE_AFFINITY_MEMBER",
    "ROLE_PAIRING_ENTRY",
    "ROLE_SUBJECT",
    "SOURCE_ROLES",
    "NameRequest",
    "NameResolution",
    "NormalizeError",
    "ResolutionCounts",
    "create_reviewed_entity",
    "is_machine_mapping_status",
    "resolve_source_names",
    "source_name_id_for",
]

ROLE_SUBJECT = "subject"
ROLE_PAIRING_ENTRY = "pairing_entry"
# CP6: affinity members resolve under their own role rather than overloading
# pairing_entry (approved CP6 decision 4; docs/DECISIONS.md §A keys mappings
# per role). source_role carries no database CHECK constraint, so this is a
# code-level vocabulary extension only — schema.sql is unchanged.
ROLE_AFFINITY_MEMBER = "affinity_member"
SOURCE_ROLES = frozenset({ROLE_SUBJECT, ROLE_PAIRING_ENTRY, ROLE_AFFINITY_MEMBER})

NORMALIZATION_STATUS_UNRESOLVED = "unresolved"
NORMALIZATION_STATUS_AUTO_MAPPED = "auto_mapped"
NORMALIZATION_STATUS_HUMAN_MAPPED = "human_mapped"
# The exact machine-editable set (approved CP5 decision 5). Anything else —
# human_mapped or an unrecognized value — is human-owned; fail closed.
MACHINE_MAPPING_STATUSES = frozenset({None, NORMALIZATION_STATUS_UNRESOLVED,
                                      NORMALIZATION_STATUS_AUTO_MAPPED})

REVIEW_STATUS_NEEDS_REVIEW = "needs_review"
REVIEW_STATUS_APPROVED = "approved"
REVIEW_STATUS_REJECTED = "rejected"
REVIEW_STATUSES = frozenset(
    {REVIEW_STATUS_NEEDS_REVIEW, REVIEW_STATUS_APPROVED, REVIEW_STATUS_REJECTED}
)

ENTITY_TYPE_UNKNOWN = "unknown"
# docs/SCHEMA.md §4.
ENTITY_TYPES = frozenset(
    {"ingredient", "cuisine", "dish", "beverage", "technique", "preparation",
     "category", ENTITY_TYPE_UNKNOWN}
)


class NormalizeError(Exception):
    """Normalization cannot proceed or a reviewed decision is invalid."""


def _sha16(parts: List[Optional[str]]) -> str:
    """First 16 hex chars of SHA-256 over an explicit canonical JSON array."""
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def source_name_id_for(source_id: str, source_text: str, source_role: str) -> str:
    """Deterministic content-derived source_name_id for one mapping key."""
    return f"sn_{_sha16([source_id, source_text, source_role])}"


def is_machine_mapping_status(status: Optional[str]) -> bool:
    """Whether a mapping row's normalization_status is machine-editable."""
    return status in MACHINE_MAPPING_STATUSES


@dataclass(frozen=True)
class NameRequest:
    """One distinct source string to resolve, with the parser's clean form."""

    source_text: str
    source_role: str
    clean_text: str


@dataclass(frozen=True)
class NameResolution:
    """The mapping state for one key after resolution."""

    source_text: str
    source_role: str
    entity_id: Optional[str]
    normalization_status: Optional[str]


@dataclass(frozen=True)
class ResolutionCounts:
    mappings_created: int
    mappings_updated: int
    unresolved: int


def create_reviewed_entity(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    canonical_name: str,
    entity_type: str = ENTITY_TYPE_UNKNOWN,
    display_name: Optional[str] = None,
    parent_entity_id: Optional[str] = None,
    review_status: str = REVIEW_STATUS_NEEDS_REVIEW,
    notes: Optional[str] = None,
) -> None:
    """Record one explicitly reviewed entity-creation decision.

    The caller supplies the ``entity_id`` — this helper generates nothing
    automatically and is never called by ``normalize_source``. The default
    ``entity_type`` is ``'unknown'``: type is only ever what the reviewer
    positively asserts, never an inference.
    """
    if not entity_id or not entity_id.strip():
        raise NormalizeError("create_reviewed_entity: entity_id must not be blank")
    if not canonical_name or not canonical_name.strip():
        raise NormalizeError("create_reviewed_entity: canonical_name must not be blank")
    if entity_type not in ENTITY_TYPES:
        raise NormalizeError(
            f"create_reviewed_entity: invalid entity_type '{entity_type}'; "
            f"expected one of {sorted(ENTITY_TYPES)}"
        )
    if review_status not in REVIEW_STATUSES:
        raise NormalizeError(
            f"create_reviewed_entity: invalid review_status '{review_status}'; "
            f"expected one of {sorted(REVIEW_STATUSES)}"
        )
    existing = connection.execute(
        "SELECT entity_id FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if existing is not None:
        raise NormalizeError(
            f"create_reviewed_entity: entity_id '{entity_id}' already exists; "
            f"existing entities are never overwritten"
        )
    connection.execute(
        "INSERT INTO entities (entity_id, canonical_name, display_name, entity_type, "
        "parent_entity_id, normalization_status, review_status, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entity_id,
            canonical_name,
            display_name,
            entity_type,
            parent_entity_id,
            NORMALIZATION_STATUS_HUMAN_MAPPED,
            review_status,
            notes,
        ),
    )
    connection.commit()


def _entity_index(connection: sqlite3.Connection) -> Dict[str, List[Tuple[str, Optional[str]]]]:
    """canonical_name (trimmed, case-folded) -> [(entity_id, review_status)]."""
    index: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    for row in connection.execute(
        "SELECT entity_id, canonical_name, review_status FROM entities "
        "WHERE canonical_name IS NOT NULL"
    ):
        key = row["canonical_name"].strip().lower()
        index.setdefault(key, []).append((row["entity_id"], row["review_status"]))
    return index


def _exact_match(
    index: Dict[str, List[Tuple[str, Optional[str]]]], clean_text: str
) -> Optional[str]:
    """The single non-rejected entity matching clean_text exactly, or None.

    Zero candidates, several candidates (ambiguous), or only rejected
    candidates all resolve to None — never a guess.
    """
    candidates = [
        entity_id
        for entity_id, review_status in index.get(clean_text, [])
        if review_status != REVIEW_STATUS_REJECTED
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_source_names(
    connection: sqlite3.Connection,
    source_id: str,
    requests: Iterable[NameRequest],
) -> Tuple[Dict[Tuple[str, str], NameResolution], ResolutionCounts]:
    """Resolve every requested name key and write mapping rows accordingly.

    Performs INSERT/UPDATE on ``entity_source_names`` only, without
    committing — the caller (``normalize_source``) owns the transaction.
    One mapping row per ``(source_id, source_text, source_role)`` key
    (docs/DECISIONS.md §A), however many raw rows repeat the string.
    """
    deduped: Dict[Tuple[str, str], str] = {}
    for request in requests:
        if request.source_role not in SOURCE_ROLES:
            raise NormalizeError(
                f"unknown source_role '{request.source_role}'; "
                f"expected one of {sorted(SOURCE_ROLES)}"
            )
        key = (request.source_text, request.source_role)
        if key in deduped and deduped[key] != request.clean_text:
            raise NormalizeError(
                f"conflicting clean texts for source_text {request.source_text!r} "
                f"role {request.source_role}: {deduped[key]!r} vs {request.clean_text!r}"
            )
        deduped[key] = request.clean_text

    existing: Dict[Tuple[str, str], sqlite3.Row] = {
        (row["source_text"], row["source_role"]): row
        for row in connection.execute(
            "SELECT source_name_id, source_text, source_role, entity_id, "
            "normalization_status FROM entity_source_names WHERE source_id = ?",
            (source_id,),
        )
    }
    index = _entity_index(connection)

    resolutions: Dict[Tuple[str, str], NameResolution] = {}
    created = updated = 0

    for key in sorted(deduped):
        source_text, source_role = key
        clean_text = deduped[key]
        row = existing.get(key)

        if row is not None:
            status = row["normalization_status"]
            entity_id = row["entity_id"]
            if not is_machine_mapping_status(status) or entity_id is not None:
                # Human-owned (fail closed on unrecognized statuses) or
                # already mapped by the machine: used as-is, never modified.
                resolutions[key] = NameResolution(source_text, source_role, entity_id, status)
                continue
            # Machine-owned, unresolved: retry the exact match.
            matched = _exact_match(index, clean_text)
            if matched is not None:
                cursor = connection.execute(
                    "UPDATE entity_source_names SET entity_id = ?, normalization_status = ? "
                    "WHERE source_name_id = ? AND entity_id IS NULL AND "
                    "(normalization_status IS NULL OR normalization_status IN (?, ?))",
                    (
                        matched,
                        NORMALIZATION_STATUS_AUTO_MAPPED,
                        row["source_name_id"],
                        NORMALIZATION_STATUS_UNRESOLVED,
                        NORMALIZATION_STATUS_AUTO_MAPPED,
                    ),
                )
                if cursor.rowcount != 1:
                    raise NormalizeError(
                        f"refused to update mapping {row['source_name_id']}: "
                        f"row is not machine-owned/unresolved as expected"
                    )
                updated += 1
                resolutions[key] = NameResolution(
                    source_text, source_role, matched, NORMALIZATION_STATUS_AUTO_MAPPED
                )
            else:
                resolutions[key] = NameResolution(source_text, source_role, None, status)
            continue

        # No existing row: insert a new machine mapping (mapped or unresolved).
        matched = _exact_match(index, clean_text)
        status = (
            NORMALIZATION_STATUS_AUTO_MAPPED if matched is not None
            else NORMALIZATION_STATUS_UNRESOLVED
        )
        connection.execute(
            "INSERT INTO entity_source_names "
            "(source_name_id, source_id, source_text, source_role, entity_id, "
            "normalization_status, notes) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                source_name_id_for(source_id, source_text, source_role),
                source_id,
                source_text,
                source_role,
                matched,
                status,
            ),
        )
        created += 1
        resolutions[key] = NameResolution(source_text, source_role, matched, status)

    unresolved = sum(1 for r in resolutions.values() if r.entity_id is None)
    return resolutions, ResolutionCounts(
        mappings_created=created, mappings_updated=updated, unresolved=unresolved
    )
