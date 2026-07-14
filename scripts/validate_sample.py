#!/usr/bin/env python3
"""Validate the committed canonical sample package (CP7).

Thin CLI wrapper over :mod:`flavor_pairing.validation` — one implementation
of the rules, not two (docs/DATA_FOUNDATION_PLAN.md §18). Validates
``data/sample/`` by default; pass a directory argument to validate another
package (e.g. a regenerated temporary copy).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # allow `python3 scripts/validate_sample.py`

from flavor_pairing.validation import validate_sample_package

DEFAULT_SAMPLE_DIR = REPO_ROOT / "data" / "sample"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample_dir", nargs="?", default=str(DEFAULT_SAMPLE_DIR),
        help="package directory to validate (default: data/sample)",
    )
    args = parser.parse_args(argv)

    result = validate_sample_package(Path(args.sample_dir))
    if result.errors:
        for error in result.errors:
            print(f"VALIDATION FAILED: {error}", file=sys.stderr)
        return 1

    print("VALIDATION PASSED")
    print(f"Raw rows: {result.counts.get('raw_source_rows.csv', 0)}")
    print(f"Parsed rows: {result.counts.get('parsed_source_rows.csv', 0)}")
    print(f"Entities: {result.counts.get('entities.csv', 0)}")
    print(f"Source-name mappings: {result.counts.get('entity_source_names.csv', 0)}")
    print(f"Pairing observations: {result.counts.get('pairing_observations.csv', 0)}")
    print(f"Attributes: {result.counts.get('entity_attributes.csv', 0)}")
    print(f"Affinity groups: {result.counts.get('affinity_groups.csv', 0)}")
    print(f"Affinity members: {result.counts.get('affinity_members.csv', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
