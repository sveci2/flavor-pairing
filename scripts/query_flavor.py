#!/usr/bin/env python3
"""Read-only CLI over the flavor-pairing query layer (CP8).

Usage:
    python3 scripts/query_flavor.py NAME [--package DIR]
        [--section all|pairings|reverse|attributes|affinities|unresolved]
        [--json]

Both output formats are rendered from the SAME top-level
``EntityQueryResult`` model (flavor_pairing.query) through the shared
serializer (flavor_pairing.serialization), so plain text and JSON cannot
diverge — and the CP9 HTTP API renders from that same serializer. JSON
preserves stored NULLs as ``null``; plain text shows them as ``-`` purely
as presentation. All operations are read-only; resolution is exact
``strip().casefold()`` lookup only.

Exit codes: 0 success; 1 entity not found or ambiguous; 2 unusable package
(QueryError). Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # allow `python3 scripts/query_flavor.py`

from flavor_pairing.query import (
    AmbiguousEntityError,
    EntityQueryResult,
    FlavorPackage,
    PairingResult,
    QueryError,
)
from flavor_pairing.serialization import SECTIONS, result_to_dict, selected_fields

DEFAULT_PACKAGE_DIR = REPO_ROOT / "data" / "sample"


def _show(value: object) -> str:
    """Plain-text presentation of one field; NULL displays as '-' only here."""
    return "-" if value is None else str(value)


def _pairing_lines(observations: Sequence[PairingResult], indent: str) -> List[str]:
    lines = []
    for position, o in enumerate(observations, start=1):
        lines.append(
            f"{indent}{position}. text={_show(o.paired_text_raw)!s} "
            f"paired_entity={_show(o.paired_entity_id)} "
            f"label={_show(o.strength_label)} score={_show(o.strength_score)} "
            f"method={_show(o.strength_method)} "
            f"norm={_show(o.normalization_status)} review={_show(o.review_status)}"
        )
        lines.append(
            f"{indent}   [source={o.source_id} record={o.source_record_id} "
            f"observation={o.observation_id} subject={o.subject_entity_id}]"
        )
    return lines


def render_text(result: EntityQueryResult, section: str = "all") -> str:
    """Plain-text rendering of the same EntityQueryResult model."""
    entity = result.entity
    lines = [
        f"Entity: {entity.entity_id}",
        f"  canonical_name={_show(entity.canonical_name)} "
        f"display_name={_show(entity.display_name)} "
        f"entity_type={_show(entity.entity_type)}",
        f"  normalization_status={_show(entity.normalization_status)} "
        f"review_status={_show(entity.review_status)} notes={_show(entity.notes)}",
    ]
    fields = selected_fields(section)

    if "pairings" in fields:
        lines.append(f"Pairings as subject ({len(result.pairings.as_subject)}):")
        lines.extend(_pairing_lines(result.pairings.as_subject, "  "))
        lines.append(f"Pairings as paired ({len(result.pairings.as_paired)}):")
        lines.extend(_pairing_lines(result.pairings.as_paired, "  "))

    if "reverse_pairs" in fields:
        lines.append(f"Reverse-pair evidence ({len(result.reverse_pairs)}):")
        for evidence in result.reverse_pairs:
            lines.append(f"  pair {evidence.entity_a} <-> {evidence.entity_b}")
            lines.append(
                f"    direction {evidence.entity_a} -> {evidence.entity_b} "
                f"({len(evidence.observations_a_to_b)}):"
            )
            lines.extend(_pairing_lines(evidence.observations_a_to_b, "      "))
            lines.append(
                f"    direction {evidence.entity_b} -> {evidence.entity_a} "
                f"({len(evidence.observations_b_to_a)}):"
            )
            lines.extend(_pairing_lines(evidence.observations_b_to_a, "      "))

    if "attributes" in fields:
        lines.append(f"Attributes ({len(result.attributes)}):")
        for position, a in enumerate(result.attributes, start=1):
            lines.append(
                f"  {position}. {_show(a.attribute_name)}={_show(a.attribute_value_raw)!s} "
                f"normalized={_show(a.attribute_value_normalized)} "
                f"method={_show(a.normalization_method)} review={_show(a.review_status)}"
            )
            lines.append(
                f"     [source={a.source_id} record={a.source_record_id} "
                f"attribute={a.attribute_id}]"
            )

    if "affinities" in fields:
        for heading, groups in (
            ("Affinity groups as subject", result.affinities.as_subject),
            ("Affinity groups as member", result.affinities.as_member),
        ):
            lines.append(f"{heading} ({len(groups)}):")
            for group in groups:
                lines.append(
                    f"  {group.affinity_id} text={_show(group.affinity_text_raw)!s} "
                    f"subject={_show(group.subject_entity_id)} "
                    f"review={_show(group.review_status)}"
                )
                lines.append(
                    f"    [source={group.source_id} record={group.source_record_id}]"
                )
                for member in group.members:
                    lines.append(
                        f"    {member.member_order}. text={_show(member.member_text_raw)!s} "
                        f"entity={_show(member.member_entity_id)} "
                        f"norm={_show(member.normalization_status)}"
                    )

    if "unresolved_mappings" in fields:
        lines.append(f"Unresolved mappings in package ({len(result.unresolved_mappings)}):")
        for position, m in enumerate(result.unresolved_mappings, start=1):
            lines.append(
                f"  {position}. text={m.source_text!r} role={m.source_role} "
                f"norm={_show(m.normalization_status)} "
                f"[source={m.source_id} mapping={m.source_name_id}]"
            )
    if "unresolved_observations" in fields:
        lines.append(
            f"Unresolved observations in package ({len(result.unresolved_observations)}):"
        )
        lines.extend(_pairing_lines(result.unresolved_observations, "  "))

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="entity canonical name (exact match after trim+casefold)")
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE_DIR),
                        help="package directory to query (default: data/sample)")
    parser.add_argument("--section", choices=SECTIONS, default="all")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON (stored NULLs stay null) instead of plain text")
    args = parser.parse_args(argv)

    try:
        package = FlavorPackage.load(Path(args.package))
        result = package.query(args.name)
    except AmbiguousEntityError as exc:
        print(f"AMBIGUOUS: {exc}", file=sys.stderr)
        return 1
    except QueryError as exc:
        print(f"PACKAGE ERROR: {exc}", file=sys.stderr)
        return 2

    if result is None:
        print(
            f"NOT FOUND: no entity has canonical name {args.name!r} "
            f"(exact match after trim+casefold; no fuzzy matching)",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result_to_dict(result, args.section),
                         indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(render_text(result, args.section))
    return 0


if __name__ == "__main__":
    sys.exit(main())
