"""Derived affinity groups and members from parsed affinity rows (CP6;
docs/SCHEMA.md §8; docs/DECISIONS.md §B; docs/DATA_FOUNDATION_PLAN.md §9,
§13).

Reconstruction rules (approved CP6 design):

- Input: current-version ``parsed_source_rows`` with
  ``row_type = 'affinity_group'`` — parser evidence only. Affinity rows are
  never reinterpreted as binary pairings; nothing here writes
  ``pairing_observations``.
- One ``affinity_groups`` row per parsed affinity row, with
  ``affinity_text_raw = entry_raw`` verbatim and full
  ``(source_id, source_record_id)`` provenance. ``affinity_id`` is
  content-derived, so rebuilds are deterministic.
- Members come only from splitting ``entry_raw`` on the format's
  **approved** ``affinity_split_rules.member_delimiter``
  (``config.require_affinity_rule``; a source with affinity rows but no
  approved rule fails fast with ``ConfigError`` and writes nothing).
  ``member_order`` is the 1-based token position; ``member_text_raw`` is
  the exact token as split, untrimmed. Every token becomes a member row —
  including one naming the subject, and including empty tokens the source
  text actually contains (docs/DECISIONS.md §B: membership reflects the
  source phrase, never an inference).
- Member matching uses ``token.strip().lower()`` — the same trim +
  case-fold semantics as the CP4 parser cleaning and the CP5 entity index,
  nothing more. Members resolve through the unchanged CP5
  ``resolve_source_names`` under the ``affinity_member`` role: exact match
  only, rejected entities excluded, machine rows only, no automatic entity
  creation. A token whose clean form is empty gets **no**
  ``entity_source_names`` row (there is nothing to map); its member row is
  kept with the exact raw token, a NULL entity, and ``'unresolved'``.
- ``subject_entity_id`` reuses the existing ``subject``-role mapping. The
  column is nullable by schema, so a group with an unresolved subject is
  **written** (never skipped, never given a placeholder), counted in
  ``groups_with_unresolved_subject``, and left ``needs_review``.
- ``review_status`` (documented vocabulary): ``'approved'`` only when the
  subject and every member resolved through ``human_mapped`` mappings;
  otherwise ``'needs_review'``. Members carry the mapping's
  ``normalization_status`` verbatim when resolved, ``'unresolved'`` when
  not. No strength scores exist on either table and none are invented.
- Rebuild is per source, in one transaction, explicitly FK-safe: this
  source's ``affinity_members`` are deleted first (via their groups), then
  its ``affinity_groups``, then fresh rows are inserted — groups before
  members. Decision tables are never deleted or overwritten; the merge
  rule of :mod:`flavor_pairing.normalize.entities` governs every mapping
  write.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flavor_pairing.config.loaders import ProjectConfig
from flavor_pairing.ingest.runs import current_version
from flavor_pairing.normalize.entities import (
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_NEEDS_REVIEW,
    ROLE_AFFINITY_MEMBER,
    ROLE_SUBJECT,
    NameRequest,
    NameResolution,
    NormalizeError,
    resolve_source_names,
)

__all__ = ["AffinityOutcome", "affinity_id_for", "normalize_affinities"]


def affinity_id_for(source_record_id: str) -> str:
    """Deterministic content-derived affinity_id for one parsed row."""
    canonical = json.dumps([source_record_id], ensure_ascii=False, separators=(",", ":"))
    return f"aff_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class AffinityOutcome:
    """What one ``normalize_affinities`` call did."""

    source_id: str
    run_id: str
    groups_written: int
    members_written: int
    unresolved_members: int
    groups_with_unresolved_subject: int
    mappings_created: int
    mappings_updated: int


@dataclass(frozen=True)
class _MemberRow:
    affinity_id: str
    member_order: int
    member_entity_id: Optional[str]
    member_text_raw: str
    normalization_status: str


@dataclass(frozen=True)
class _GroupRow:
    affinity_id: str
    source_id: str
    source_record_id: str
    subject_entity_id: Optional[str]
    affinity_text_raw: str
    review_status: str


def _member_resolution(
    resolutions: Dict[Tuple[str, str], NameResolution], token: str
) -> Tuple[Optional[str], str, bool]:
    """(entity_id, normalization_status, human_resolved) for one raw token."""
    clean = token.strip().lower()
    if not clean:
        return None, NORMALIZATION_STATUS_UNRESOLVED, False
    resolution = resolutions[(token, ROLE_AFFINITY_MEMBER)]
    if resolution.entity_id is None:
        return None, NORMALIZATION_STATUS_UNRESOLVED, False
    human = resolution.normalization_status == NORMALIZATION_STATUS_HUMAN_MAPPED
    return resolution.entity_id, resolution.normalization_status, human


def normalize_affinities(
    connection: sqlite3.Connection, config: ProjectConfig, source_id: str
) -> AffinityOutcome:
    """Rebuild ``affinity_groups``/``affinity_members`` for one source."""
    source = config.source(source_id)

    version = current_version(connection, source_id)
    if version is None:
        raise NormalizeError(
            f"source '{source_id}' has no completed import run; "
            f"ingest and parse it before normalizing affinities"
        )
    parsed_keys = {
        row["source_record_id"]
        for row in connection.execute(
            "SELECT source_record_id FROM parsed_source_rows WHERE source_id = ?",
            (source_id,),
        )
    }
    if not parsed_keys:
        raise NormalizeError(
            f"source '{source_id}' has no parsed rows; run the parser before "
            f"normalizing affinities"
        )
    member_keys = {source_record_id for source_record_id, _ in version.members}
    if parsed_keys != member_keys:
        raise NormalizeError(
            f"parsed rows for source '{source_id}' do not match its current "
            f"version (run '{version.run_id}'); re-run the parser before "
            f"normalizing affinities"
        )

    affinity_rows = connection.execute(
        "SELECT p.source_record_id, p.subject_clean, r.subject_raw, r.entry_raw "
        "FROM parsed_source_rows p JOIN raw_source_rows r "
        "ON p.source_id = r.source_id AND p.source_record_id = r.source_record_id "
        "WHERE p.source_id = ? AND p.row_type = 'affinity_group' "
        "ORDER BY p.source_record_id",
        (source_id,),
    ).fetchall()

    delimiter: Optional[str] = None
    if affinity_rows:
        # Only approved rules may drive splitting; fails fast (ConfigError)
        # before any write when the rule is missing or unapproved.
        delimiter = config.require_affinity_rule(source.source_format).member_delimiter

    requests: Dict[Tuple[str, str], NameRequest] = {}
    for row in affinity_rows:
        requests.setdefault(
            (row["subject_raw"], ROLE_SUBJECT),
            NameRequest(row["subject_raw"], ROLE_SUBJECT, row["subject_clean"]),
        )
        for token in row["entry_raw"].split(delimiter):
            clean = token.strip().lower()
            if clean:  # empty-clean tokens get no mapping row: nothing to map
                requests.setdefault(
                    (token, ROLE_AFFINITY_MEMBER),
                    NameRequest(token, ROLE_AFFINITY_MEMBER, clean),
                )

    try:
        resolutions, counts = resolve_source_names(
            connection, source_id, requests.values()
        )

        group_rows: List[_GroupRow] = []
        member_rows: List[_MemberRow] = []
        unresolved_members = 0
        unresolved_subjects = 0
        for row in affinity_rows:
            affinity_id = affinity_id_for(row["source_record_id"])
            subject = resolutions[(row["subject_raw"], ROLE_SUBJECT)]
            subject_human = (
                subject.entity_id is not None
                and subject.normalization_status == NORMALIZATION_STATUS_HUMAN_MAPPED
            )
            if subject.entity_id is None:
                unresolved_subjects += 1

            all_members_human = True
            for order, token in enumerate(row["entry_raw"].split(delimiter), start=1):
                entity_id, status, human = _member_resolution(resolutions, token)
                if entity_id is None:
                    unresolved_members += 1
                all_members_human = all_members_human and human
                member_rows.append(
                    _MemberRow(
                        affinity_id=affinity_id,
                        member_order=order,
                        member_entity_id=entity_id,
                        member_text_raw=token,
                        normalization_status=status,
                    )
                )

            group_rows.append(
                _GroupRow(
                    affinity_id=affinity_id,
                    source_id=source_id,
                    source_record_id=row["source_record_id"],
                    subject_entity_id=subject.entity_id,
                    affinity_text_raw=row["entry_raw"],
                    review_status=(
                        REVIEW_STATUS_APPROVED
                        if subject_human and all_members_human
                        else REVIEW_STATUS_NEEDS_REVIEW
                    ),
                )
            )

        # Explicit FK-safe rebuild order: members of this source's groups
        # first, then the groups, then fresh groups before fresh members.
        connection.execute(
            "DELETE FROM affinity_members WHERE affinity_id IN "
            "(SELECT affinity_id FROM affinity_groups WHERE source_id = ?)",
            (source_id,),
        )
        connection.execute(
            "DELETE FROM affinity_groups WHERE source_id = ?", (source_id,)
        )
        for group in group_rows:
            connection.execute(
                "INSERT INTO affinity_groups (affinity_id, source_id, "
                "source_record_id, subject_entity_id, affinity_text_raw, "
                "review_status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group.affinity_id,
                    group.source_id,
                    group.source_record_id,
                    group.subject_entity_id,
                    group.affinity_text_raw,
                    group.review_status,
                ),
            )
        for member in member_rows:
            connection.execute(
                "INSERT INTO affinity_members (affinity_id, member_order, "
                "member_entity_id, member_text_raw, normalization_status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    member.affinity_id,
                    member.member_order,
                    member.member_entity_id,
                    member.member_text_raw,
                    member.normalization_status,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return AffinityOutcome(
        source_id=source_id,
        run_id=version.run_id,
        groups_written=len(group_rows),
        members_written=len(member_rows),
        unresolved_members=unresolved_members,
        groups_with_unresolved_subject=unresolved_subjects,
        mappings_created=counts.mappings_created,
        mappings_updated=counts.mappings_updated,
    )
