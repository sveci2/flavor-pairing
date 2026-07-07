#!/usr/bin/env python3
"""Convert a supported external CSV into immutable raw_source_rows.csv.

This script intentionally does not normalize ingredient names or scores.
"""

from pathlib import Path
import argparse
import csv
import json

COLUMN_MAPS = {
    "main_pairing_csv": {
        "subject": "main",
        "entry": "pairing",
        "quality": None,
    },
    "pair_quality_csv": {
        "subject": "ingredient1",
        "entry": "ingredient2",
        "quality": "quality",
    },
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--format", choices=COLUMN_MAPS, required=True)
    args = parser.parse_args()

    mapping = COLUMN_MAPS[args.format]
    with open(args.input_csv, newline="", encoding="utf-8-sig") as src:
        rows = list(csv.DictReader(src))

    columns = [
        "source_id","source_record_id","source_order","subject_raw",
        "entry_raw","quality_raw","raw_payload_json"
    ]
    with open(args.output_csv, "w", newline="", encoding="utf-8-sig") as dst:
        writer = csv.DictWriter(dst, fieldnames=columns)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow({
                "source_id": args.source_id,
                "source_record_id": f"{args.source_id}_{index:07d}",
                "source_order": index,
                "subject_raw": row.get(mapping["subject"], ""),
                "entry_raw": row.get(mapping["entry"], ""),
                "quality_raw": row.get(mapping["quality"], "") if mapping["quality"] else "",
                "raw_payload_json": json.dumps(row, ensure_ascii=False),
            })

if __name__ == "__main__":
    main()
