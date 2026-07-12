"""Typography/marker detection for strength evidence (CP4;
docs/DATA_FOUNDATION_PLAN.md §11).

Pure functions only — no database, filesystem, or configuration access. The
detector maps one raw row's ``(entry_raw, quality_raw)`` to exactly one
marker key from the closed set defined in
:mod:`flavor_pairing.config.loaders` (``plain``, ``uppercase``,
``asterisk_uppercase``, ``explicit_label:<value>``); it can emit nothing
else. Extending the key set is a reviewed change to the config loader, never
a local addition here.

Detection rules (approved CP4 design):

- A non-blank ``quality_raw`` is an explicit quality label and always takes
  precedence over entry typography. The key is
  ``explicit_label:<quality_raw.strip()>`` — the exact stripped value, with
  no case-folding: an unexpected casing becomes an unmapped key that the
  strength resolver surfaces for review rather than a silently folded match.
- One or more leading asterisks plus a fully uppercase remainder is
  ``asterisk_uppercase``; fully uppercase text without an asterisk is
  ``uppercase`` (``str.isupper()`` semantics: at least one cased character,
  no lowercase ones).
- Everything else is ``plain`` — which a typography-lossy format's
  configuration maps to no score at all (docs/DECISIONS.md §F).
- A leading asterisk whose remainder is *not* fully uppercase does not match
  any scored marker; the closed set forces ``plain`` (null score — nothing
  is invented), and the result is flagged ``ambiguous`` so the parser marks
  the row ``requires_review = 1`` and the suspicious marker stays visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from flavor_pairing.config.loaders import EXPLICIT_LABEL_PREFIX

__all__ = [
    "MARKER_ASTERISK_UPPERCASE",
    "MARKER_PLAIN",
    "MARKER_UPPERCASE",
    "MARKER_RAW_ASTERISK_UPPERCASE",
    "MARKER_RAW_UPPERCASE",
    "TypographyMarker",
    "detect_marker",
]

MARKER_PLAIN = "plain"
MARKER_UPPERCASE = "uppercase"
MARKER_ASTERISK_UPPERCASE = "asterisk_uppercase"

# Human-readable strength_marker_raw values (sample-data convention).
MARKER_RAW_UPPERCASE = "uppercase"
MARKER_RAW_ASTERISK_UPPERCASE = "*+uppercase"


@dataclass(frozen=True)
class TypographyMarker:
    """One detection result: the machine key, the human-readable evidence,
    and whether the typography was marker-like but unresolvable."""

    marker_key: str
    strength_marker_raw: Optional[str]
    ambiguous: bool = False


def detect_marker(entry_raw: str, quality_raw: Optional[str]) -> TypographyMarker:
    """Detect the strength marker for one row's entry/quality values."""
    quality = (quality_raw or "").strip()
    if quality:
        return TypographyMarker(
            marker_key=f"{EXPLICIT_LABEL_PREFIX}{quality}",
            strength_marker_raw=quality,
        )

    text = entry_raw.strip()
    without_asterisks = text.lstrip("*")
    has_asterisk = len(without_asterisks) != len(text)

    if has_asterisk and without_asterisks.isupper():
        return TypographyMarker(
            marker_key=MARKER_ASTERISK_UPPERCASE,
            strength_marker_raw=MARKER_RAW_ASTERISK_UPPERCASE,
        )
    if not has_asterisk and text.isupper():
        return TypographyMarker(
            marker_key=MARKER_UPPERCASE,
            strength_marker_raw=MARKER_RAW_UPPERCASE,
        )
    if has_asterisk:
        return TypographyMarker(
            marker_key=MARKER_PLAIN, strength_marker_raw=None, ambiguous=True
        )
    return TypographyMarker(marker_key=MARKER_PLAIN, strength_marker_raw=None)
