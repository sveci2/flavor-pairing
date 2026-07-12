"""Review-queue report over existing tables (CP5;
docs/DATA_FOUNDATION_PLAN.md §7).

The queue is a **read-only report** — nothing needing review is stored
twice, and building the queue never writes anything. Resolution happens by
editing decision tables (or via the reviewed-creation helper), after which
rerunning normalization shrinks the queue because resolved rows no longer
match these predicates.

Predicates (approved CP5 design):

- ``unresolved_mapping`` — every ``entity_source_names`` row with
  ``entity_id IS NULL``, **regardless of normalization_status**: a null
  mapping stays visible even when human-owned or carrying an unrecognized
  status; the status is shown in the item detail so a reviewer can tell
  them apart.
- ``unclassified_row`` — ``parsed_source_rows.row_type = 'unclassified'``.
- ``requires_review_row`` — ``parsed_source_rows.requires_review = 1`` and
  ``row_type != 'unclassified'`` (an unclassified row is never listed
  twice).
- ``entity_needs_review`` — ``entities.review_status = 'needs_review'``.
- ``unresolved_paired_entity`` — ``pairing_observations`` rows whose
  ``paired_entity_id IS NULL``.
- ``unresolved_affinity_subject`` (CP6) — ``affinity_groups`` rows whose
  ``subject_entity_id IS NULL``.
- ``unresolved_affinity_member`` (CP6) — ``affinity_members`` rows whose
  ``member_entity_id IS NULL``.

The two CP6 affinity predicates are row-level conditions; the corresponding
``unresolved_mapping`` items remain listed as well — they are a separate,
decision-table review grain (approved CP6 decision 6).

Ordering is fully deterministic: items are sorted by
``(table, reason, source_id, item_key)`` tuples, independent of insertion
order.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional

__all__ = [
    "QUEUE_TABLES",
    "REASON_ENTITY_NEEDS_REVIEW",
    "REASON_REQUIRES_REVIEW_ROW",
    "REASON_UNCLASSIFIED_ROW",
    "REASON_UNRESOLVED_AFFINITY_MEMBER",
    "REASON_UNRESOLVED_AFFINITY_SUBJECT",
    "REASON_UNRESOLVED_MAPPING",
    "REASON_UNRESOLVED_PAIRED",
    "ReviewItem",
    "build_review_queue",
]

REASON_UNRESOLVED_MAPPING = "unresolved_mapping"
REASON_UNCLASSIFIED_ROW = "unclassified_row"
REASON_REQUIRES_REVIEW_ROW = "requires_review_row"
REASON_ENTITY_NEEDS_REVIEW = "entity_needs_review"
REASON_UNRESOLVED_PAIRED = "unresolved_paired_entity"
REASON_UNRESOLVED_AFFINITY_SUBJECT = "unresolved_affinity_subject"
REASON_UNRESOLVED_AFFINITY_MEMBER = "unresolved_affinity_member"

QUEUE_TABLES = (
    "affinity_groups",
    "affinity_members",
    "entities",
    "entity_source_names",
    "pairing_observations",
    "parsed_source_rows",
)


@dataclass(frozen=True)
class ReviewItem:
    """One outstanding review item, identified by table, reason, and key."""

    table: str
    reason: str
    source_id: Optional[str]
    item_key: str
    detail: str


def build_review_queue(
    connection: sqlite3.Connection, table: Optional[str] = None
) -> List[ReviewItem]:
    """All outstanding review items, deterministically ordered.

    ``table`` optionally narrows the report to one table's items.
    """
    if table is not None and table not in QUEUE_TABLES:
        raise ValueError(
            f"unknown review-queue table '{table}'; expected one of {list(QUEUE_TABLES)}"
        )

    items: List[ReviewItem] = []

    for row in connection.execute(
        "SELECT source_id, source_text, source_role, normalization_status "
        "FROM entity_source_names WHERE entity_id IS NULL"
    ):
        status = row["normalization_status"]
        items.append(
            ReviewItem(
                table="entity_source_names",
                reason=REASON_UNRESOLVED_MAPPING,
                source_id=row["source_id"],
                item_key=f"{row['source_text']}|{row['source_role']}",
                detail=(
                    f"source_text={row['source_text']!r} role={row['source_role']} "
                    f"normalization_status={status if status is not None else '(none)'}"
                ),
            )
        )

    for row in connection.execute(
        "SELECT source_id, source_record_id, subject_clean, entry_clean "
        "FROM parsed_source_rows WHERE row_type = 'unclassified'"
    ):
        items.append(
            ReviewItem(
                table="parsed_source_rows",
                reason=REASON_UNCLASSIFIED_ROW,
                source_id=row["source_id"],
                item_key=row["source_record_id"],
                detail=f"subject={row['subject_clean']!r} entry={row['entry_clean']!r}",
            )
        )

    for row in connection.execute(
        "SELECT source_id, source_record_id, row_type, parser_confidence "
        "FROM parsed_source_rows WHERE requires_review = 1 "
        "AND row_type != 'unclassified'"
    ):
        items.append(
            ReviewItem(
                table="parsed_source_rows",
                reason=REASON_REQUIRES_REVIEW_ROW,
                source_id=row["source_id"],
                item_key=row["source_record_id"],
                detail=(
                    f"row_type={row['row_type']} "
                    f"parser_confidence={row['parser_confidence']}"
                ),
            )
        )

    for row in connection.execute(
        "SELECT entity_id, canonical_name, entity_type FROM entities "
        "WHERE review_status = 'needs_review'"
    ):
        items.append(
            ReviewItem(
                table="entities",
                reason=REASON_ENTITY_NEEDS_REVIEW,
                source_id=None,
                item_key=row["entity_id"],
                detail=(
                    f"canonical_name={row['canonical_name']!r} "
                    f"entity_type={row['entity_type']}"
                ),
            )
        )

    for row in connection.execute(
        "SELECT observation_id, source_id, paired_text_raw FROM pairing_observations "
        "WHERE paired_entity_id IS NULL"
    ):
        items.append(
            ReviewItem(
                table="pairing_observations",
                reason=REASON_UNRESOLVED_PAIRED,
                source_id=row["source_id"],
                item_key=row["observation_id"],
                detail=f"paired_text_raw={row['paired_text_raw']!r}",
            )
        )

    for row in connection.execute(
        "SELECT affinity_id, source_id, affinity_text_raw FROM affinity_groups "
        "WHERE subject_entity_id IS NULL"
    ):
        items.append(
            ReviewItem(
                table="affinity_groups",
                reason=REASON_UNRESOLVED_AFFINITY_SUBJECT,
                source_id=row["source_id"],
                item_key=row["affinity_id"],
                detail=f"affinity_text_raw={row['affinity_text_raw']!r}",
            )
        )

    for row in connection.execute(
        "SELECT m.affinity_id, m.member_order, m.member_text_raw, g.source_id "
        "FROM affinity_members m JOIN affinity_groups g "
        "ON m.affinity_id = g.affinity_id WHERE m.member_entity_id IS NULL"
    ):
        items.append(
            ReviewItem(
                table="affinity_members",
                reason=REASON_UNRESOLVED_AFFINITY_MEMBER,
                source_id=row["source_id"],
                item_key=f"{row['affinity_id']}|{row['member_order']:04d}",
                detail=f"member_text_raw={row['member_text_raw']!r}",
            )
        )

    items.sort(key=lambda item: (item.table, item.reason, item.source_id or "", item.item_key))
    if table is not None:
        items = [item for item in items if item.table == table]
    return items
