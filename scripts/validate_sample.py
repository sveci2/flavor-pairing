#!/usr/bin/env python3
from pathlib import Path
import csv
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "sample"

def read(name):
    with (DATA / name).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def require_unique(rows, field, name):
    values = [r[field] for r in rows]
    if len(values) != len(set(values)):
        raise ValueError(f"{name}: duplicate {field}")

def main():
    sources = read("sources.csv")
    raw = read("raw_source_rows.csv")
    parsed = read("parsed_source_rows.csv")
    entities = read("entities.csv")
    names = read("entity_source_names.csv")
    attrs = read("entity_attributes.csv")
    pairs = read("pairing_observations.csv")
    affinities = read("affinity_groups.csv")
    members = read("affinity_members.csv")

    require_unique(sources, "source_id", "sources")
    require_unique(raw, "source_record_id", "raw source rows")
    require_unique(entities, "entity_id", "entities")

    source_ids = {r["source_id"] for r in sources}
    record_ids = {r["source_record_id"] for r in raw}
    entity_ids = {r["entity_id"] for r in entities}
    affinity_ids = {r["affinity_id"] for r in affinities}

    for row in raw:
        if row["source_id"] not in source_ids:
            raise ValueError("Raw row has unknown source.")
        if not row["subject_raw"] or not row["entry_raw"]:
            raise ValueError("Raw rows require subject_raw and entry_raw.")

    parsed_keys = {(r["source_id"], r["source_record_id"]) for r in parsed}
    raw_keys = {(r["source_id"], r["source_record_id"]) for r in raw}
    if parsed_keys != raw_keys:
        raise ValueError("Every raw row must have one parsed row in the sample output.")

    for row in attrs:
        if row["source_record_id"] not in record_ids:
            raise ValueError("Attribute source record missing.")
        if row["entity_id"] not in entity_ids:
            raise ValueError("Attribute entity missing.")

    for row in pairs:
        if row["source_record_id"] not in record_ids:
            raise ValueError("Pair source record missing.")
        if row["subject_entity_id"] not in entity_ids:
            raise ValueError("Pair subject missing.")
        if row["paired_entity_id"] and row["paired_entity_id"] not in entity_ids:
            raise ValueError("Pair target missing.")
        if row["strength_score"] and int(row["strength_score"]) not in {1,2,3,4}:
            raise ValueError("Invalid normalized strength.")
        if row["strength_method"] == "unavailable" and row["strength_score"]:
            raise ValueError("Unavailable strength method cannot contain a score.")

    for row in members:
        if row["affinity_id"] not in affinity_ids:
            raise ValueError("Affinity member has missing group.")
        if row["member_entity_id"] and row["member_entity_id"] not in entity_ids:
            raise ValueError("Affinity member entity missing.")

    # Key reliability rule: unmarked/lowercase main_pairing rows must not be assigned a score.
    raw_by_id = {r["source_record_id"]: r for r in raw}
    for row in pairs:
        rr = raw_by_id[row["source_record_id"]]
        if rr["source_id"] == "src_synthetic_main_pairing":
            entry = rr["entry_raw"].strip()
            if not entry.startswith("*") and entry != entry.upper() and row["strength_score"]:
                raise ValueError("Lowercase typography-lossy row was assigned an invented score.")

    print("VALIDATION PASSED")
    print(f"Raw rows: {len(raw)}")
    print(f"Parsed rows: {len(parsed)}")
    print(f"Entities: {len(entities)}")
    print(f"Pairing observations: {len(pairs)}")
    print(f"Attributes: {len(attrs)}")
    print(f"Affinity groups: {len(affinities)}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
