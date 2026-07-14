"""Parse layer: stateful row classification, typography/marker detection,
and marker-key strength resolution over a source's current version (CP4
scope — docs/DATA_FOUNDATION_PLAN.md §5, §11).

Public API re-exported from :mod:`flavor_pairing.parse.row_parser`,
:mod:`flavor_pairing.parse.typography`, and
:mod:`flavor_pairing.parse.strength`. Entity normalization, observation and
attribute generation, affinity-member splitting, and the review workflow
remain out of scope here — see ``docs/DATA_FOUNDATION_PLAN.md`` §20-21.
"""

from flavor_pairing.parse.row_parser import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    PARSER_CONFIDENCES,
    ROW_TYPES,
    ROW_TYPE_AFFINITY_GROUP,
    ROW_TYPE_AFFINITY_HEADER,
    ROW_TYPE_ATTRIBUTE,
    ROW_TYPE_NOTE,
    ROW_TYPE_PAIRING_CANDIDATE,
    ROW_TYPE_UNCLASSIFIED,
    ParsedRow,
    ParseError,
    ParseInputRow,
    ParseOutcome,
    classify_rows,
    parse_source,
)
from flavor_pairing.parse.strength import (
    STRENGTH_METHODS,
    STRENGTH_METHOD_EXPLICIT,
    STRENGTH_METHOD_TYPOGRAPHIC,
    STRENGTH_METHOD_UNAVAILABLE,
    StrengthResolution,
    resolve_strength,
)
from flavor_pairing.parse.typography import (
    MARKER_ASTERISK_UPPERCASE,
    MARKER_PLAIN,
    MARKER_UPPERCASE,
    MARKER_RAW_ASTERISK_UPPERCASE,
    MARKER_RAW_UPPERCASE,
    TypographyMarker,
    detect_marker,
)

__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "PARSER_CONFIDENCES",
    "ROW_TYPES",
    "ROW_TYPE_AFFINITY_GROUP",
    "ROW_TYPE_AFFINITY_HEADER",
    "ROW_TYPE_ATTRIBUTE",
    "ROW_TYPE_NOTE",
    "ROW_TYPE_PAIRING_CANDIDATE",
    "ROW_TYPE_UNCLASSIFIED",
    "ParsedRow",
    "ParseError",
    "ParseInputRow",
    "ParseOutcome",
    "classify_rows",
    "parse_source",
    "STRENGTH_METHODS",
    "STRENGTH_METHOD_EXPLICIT",
    "STRENGTH_METHOD_TYPOGRAPHIC",
    "STRENGTH_METHOD_UNAVAILABLE",
    "StrengthResolution",
    "resolve_strength",
    "MARKER_ASTERISK_UPPERCASE",
    "MARKER_PLAIN",
    "MARKER_UPPERCASE",
    "MARKER_RAW_ASTERISK_UPPERCASE",
    "MARKER_RAW_UPPERCASE",
    "TypographyMarker",
    "detect_marker",
]
