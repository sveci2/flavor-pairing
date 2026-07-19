"""Shared JSON-ready serialization of query-layer results (CP8/CP9).

Standard library only. Every output surface — CLI plain text, CLI JSON,
the HTTP API — must render from the same top-level ``EntityQueryResult``
model through these helpers, so the formats cannot diverge. Stored NULLs
stay ``None`` and must reach JSON as ``null``; tuples become lists (JSON
has no tuple); nothing is aggregated, defaulted, or reinterpreted.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Tuple, Union

from flavor_pairing.query import EntityQueryResult

__all__ = [
    "DataclassValue",
    "JSONScalar",
    "JSONValue",
    "SECTIONS",
    "result_to_dict",
    "selected_fields",
    "to_json_value",
]

# JSON value types (Python 3.9-compatible typing.Union spelling; PEP 604
# unions would be evaluated at runtime and are 3.10+ only).
JSONScalar = Union[None, bool, int, float, str]
JSONValue = Union[JSONScalar, List["JSONValue"], Dict[str, "JSONValue"]]
# What dataclasses.asdict() yields for EntityQueryResult: like JSONValue,
# but tuples may appear wherever the models hold tuples.
DataclassValue = Union[
    JSONScalar,
    Tuple["DataclassValue", ...],
    List["DataclassValue"],
    Dict[str, "DataclassValue"],
]

# The selectable output sections: "all" plus one name per result view.
SECTIONS = ("all", "pairings", "reverse", "attributes", "affinities", "unresolved")

# EntityQueryResult field names per section (the entity itself is always shown).
_SECTION_FIELDS: Dict[str, Tuple[str, ...]] = {
    "pairings": ("pairings",),
    "reverse": ("reverse_pairs",),
    "attributes": ("attributes",),
    "affinities": ("affinities",),
    "unresolved": ("unresolved_mappings", "unresolved_observations"),
}
_ALL_FIELDS: Tuple[str, ...] = (
    "pairings", "reverse_pairs", "attributes", "affinities",
    "unresolved_mappings", "unresolved_observations",
)


def selected_fields(section: str) -> Tuple[str, ...]:
    """The EntityQueryResult field names selected by ``section``."""
    return _ALL_FIELDS if section == "all" else _SECTION_FIELDS[section]


def to_json_value(value: DataclassValue) -> JSONValue:
    """Recursively convert dataclass output to JSON-native containers.

    Tuples become lists (JSON has no tuple), dict keys stay strings, and
    scalars — including None, which must remain JSON null — pass through
    unchanged.
    """
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_value(item) for item in value]
    return value


def result_to_dict(result: EntityQueryResult, section: str = "all") -> Dict[str, JSONValue]:
    """The JSON-ready view of one EntityQueryResult (NULLs stay null)."""
    full = dataclasses.asdict(result)
    payload: Dict[str, JSONValue] = {"entity": to_json_value(full["entity"])}
    for field_name in selected_fields(section):
        payload[field_name] = to_json_value(full[field_name])
    return payload
