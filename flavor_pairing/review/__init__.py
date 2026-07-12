"""Review layer: the read-only review-queue report (CP5 scope —
docs/DATA_FOUNDATION_PLAN.md §7).

Public API re-exported from :mod:`flavor_pairing.review.queue`. Resolution
itself is a decision-table edit; no review UI or resolve tooling lives here
(docs/DATA_FOUNDATION_PLAN.md §20-21).
"""

from flavor_pairing.review.queue import (
    QUEUE_TABLES,
    REASON_ENTITY_NEEDS_REVIEW,
    REASON_REQUIRES_REVIEW_ROW,
    REASON_UNCLASSIFIED_ROW,
    REASON_UNRESOLVED_MAPPING,
    REASON_UNRESOLVED_PAIRED,
    ReviewItem,
    build_review_queue,
)

__all__ = [
    "QUEUE_TABLES",
    "REASON_ENTITY_NEEDS_REVIEW",
    "REASON_REQUIRES_REVIEW_ROW",
    "REASON_UNCLASSIFIED_ROW",
    "REASON_UNRESOLVED_MAPPING",
    "REASON_UNRESOLVED_PAIRED",
    "ReviewItem",
    "build_review_queue",
]
