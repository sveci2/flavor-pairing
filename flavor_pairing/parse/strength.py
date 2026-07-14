"""Marker-key strength resolution against ``strength_mappings.csv`` (CP4;
docs/DATA_FOUNDATION_PLAN.md §11; docs/DECISIONS.md §F).

The resolver is the only parse-layer code that reads
:class:`~flavor_pairing.config.loaders.StrengthMapping` rows for scoring, so
every scoring decision is auditable back to one config row. Rules (approved
CP4 design):

- The ``marker_key`` is matched **exactly** against the format's configured
  mappings; the human-readable ``source_value_or_marker`` column is never
  consulted.
- A mapped row with a label and score yields that label and score verbatim.
- A mapped row with a blank score (e.g. ``plain`` in a typography-lossy
  format) yields no label, no score, and method ``unavailable`` — nothing
  may substitute a default (the anti-fabrication rule, keyed on format).
- An unmapped marker key yields no label, no score, method ``unavailable``,
  and ``mapped=False`` so the caller can route the row to review. No score
  is ever guessed for unmapped evidence.
- There is no fallback/default branch of any kind.

``strength_method`` vocabulary (matches the sample data and
``scripts/validate_sample.py``): ``explicit_quality_label`` for scored
``explicit_label:*`` markers, ``typographic_marker`` for any other scored
marker, ``unavailable`` whenever no score was resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from flavor_pairing.config.loaders import EXPLICIT_LABEL_PREFIX, StrengthMapping

__all__ = [
    "STRENGTH_METHODS",
    "STRENGTH_METHOD_EXPLICIT",
    "STRENGTH_METHOD_TYPOGRAPHIC",
    "STRENGTH_METHOD_UNAVAILABLE",
    "StrengthResolution",
    "resolve_strength",
]

STRENGTH_METHOD_EXPLICIT = "explicit_quality_label"
STRENGTH_METHOD_TYPOGRAPHIC = "typographic_marker"
STRENGTH_METHOD_UNAVAILABLE = "unavailable"
STRENGTH_METHODS = frozenset(
    {STRENGTH_METHOD_EXPLICIT, STRENGTH_METHOD_TYPOGRAPHIC, STRENGTH_METHOD_UNAVAILABLE}
)


@dataclass(frozen=True)
class StrengthResolution:
    """The outcome of resolving one marker key for one source format.

    ``mapped`` distinguishes "the config explicitly declares this marker
    carries no score" (``True`` with a null score) from "the config has no
    row for this marker at all" (``False`` — the caller must flag the row
    for review).
    """

    strength_label: Optional[str]
    strength_score: Optional[int]
    strength_method: str
    mapped: bool


def resolve_strength(
    format_mappings: Mapping[str, StrengthMapping], marker_key: str
) -> StrengthResolution:
    """Resolve one marker key against one format's strength mappings."""
    mapping = format_mappings.get(marker_key)
    if mapping is None:
        return StrengthResolution(
            strength_label=None,
            strength_score=None,
            strength_method=STRENGTH_METHOD_UNAVAILABLE,
            mapped=False,
        )
    if mapping.normalized_score is None:
        return StrengthResolution(
            strength_label=None,
            strength_score=None,
            strength_method=STRENGTH_METHOD_UNAVAILABLE,
            mapped=True,
        )
    method = (
        STRENGTH_METHOD_EXPLICIT
        if marker_key.startswith(EXPLICIT_LABEL_PREFIX)
        else STRENGTH_METHOD_TYPOGRAPHIC
    )
    return StrengthResolution(
        strength_label=mapping.normalized_label,
        strength_score=mapping.normalized_score,
        strength_method=method,
        mapped=True,
    )
