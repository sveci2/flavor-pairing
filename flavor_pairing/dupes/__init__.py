"""Dupes layer: read-only duplicate and reverse-pair reports (CP6 scope —
docs/DATA_FOUNDATION_PLAN.md §8).

Public API re-exported from :mod:`flavor_pairing.dupes.detect`. Reports
never mutate, merge, or delete observations; score aggregation belongs to
the out-of-scope ``pairing_edges`` layer (docs/DATA_FOUNDATION_PLAN.md
§20-21).
"""

from flavor_pairing.dupes.detect import (
    DuplicateObservationGroup,
    ObservationRef,
    RawTextDuplicateGroup,
    ReversePairCandidate,
    duplicate_observation_report,
    raw_text_duplicate_report,
    reverse_pair_report,
)

__all__ = [
    "DuplicateObservationGroup",
    "ObservationRef",
    "RawTextDuplicateGroup",
    "ReversePairCandidate",
    "duplicate_observation_report",
    "raw_text_duplicate_report",
    "reverse_pair_report",
]
