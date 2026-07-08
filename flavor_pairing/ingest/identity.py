"""Content-derived source-record identity (docs/DECISIONS.md §H;
docs/DATA_FOUNDATION_PLAN.md §3).

Pure functions only — no database or filesystem access. Identity is derived
strictly from row content, never from row position, so inserting, removing,
or reordering rows in a later version never shifts any other row's identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

__all__ = ["RawRowContent", "assign_source_record_ids", "content_hash16"]


@dataclass(frozen=True)
class RawRowContent:
    """The four fields that determine a raw row's identity.

    ``quality_raw`` and ``raw_payload_json`` use ``None`` for "not present"
    (the project's null convention — a CSV-blank field is ``None``, never
    ``""``), since the hash must distinguish "no payload" from an actual
    empty-string payload.
    """

    subject_raw: str
    entry_raw: str
    quality_raw: Optional[str] = None
    raw_payload_json: Optional[str] = None


def content_hash16(
    subject_raw: str,
    entry_raw: str,
    quality_raw: Optional[str],
    raw_payload_json: Optional[str],
) -> str:
    """First 16 hex characters of SHA-256 over the row's canonical content.

    Hashed exactly as received: no trimming, case-folding, or other
    normalization before hashing (docs/DECISIONS.md §H).
    """
    canonical = json.dumps(
        [subject_raw, entry_raw, quality_raw, raw_payload_json],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def assign_source_record_ids(source_id: str, rows: Sequence[RawRowContent]) -> List[str]:
    """Compute ``source_record_id`` for every row of one version, in order.

    ``source_record_id = f"{source_id}:{sha256_16}:{occurrence_index}"``.
    ``occurrence_index`` is a 1-based counter over rows sharing the same
    content hash, counted in the order given here (i.e. file order for this
    version). It is recomputed fresh for every call — nothing is persisted
    between versions — because rows with the same hash are byte-identical,
    so which physical row gets index 1 vs 2 across versions is
    interchangeable by definition.
    """
    occurrence_counts: Dict[str, int] = {}
    record_ids: List[str] = []
    for row in rows:
        content_hash = content_hash16(
            row.subject_raw, row.entry_raw, row.quality_raw, row.raw_payload_json
        )
        occurrence_counts[content_hash] = occurrence_counts.get(content_hash, 0) + 1
        occurrence_index = occurrence_counts[content_hash]
        record_ids.append(f"{source_id}:{content_hash}:{occurrence_index}")
    return record_ids
