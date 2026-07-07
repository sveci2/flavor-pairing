# Data contract

## 1. `sources.csv`

Registry of provenance and rights status.

## 2. `raw_source_rows.csv`

Immutable staging table.

Required source content:

- `source_id`
- `source_record_id`
- `source_order`
- `subject_raw`
- `entry_raw`

Optional:

- `quality_raw`
- `raw_payload_json`

Never overwrite raw text during cleaning.

## 3. `parsed_source_rows.csv`

Derived parser output. `row_type` may be:

- `attribute`
- `pairing_candidate`
- `affinity_header`
- `affinity_group`
- `note`
- `unclassified`

Uncertain rows must remain available and be marked for review.

## 4. `entities.csv`

Canonical concepts used by the product. The source may contain more than ingredients, so `entity_type` must support:

- ingredient
- cuisine
- dish
- beverage
- technique
- preparation
- category
- unknown

## 5. `entity_source_names.csv`

Maps original source strings to canonical entities. A mapping can remain unresolved.

## 6. `entity_attributes.csv`

Stores named source attributes as key/value observations rather than assuming every entity has every attribute.

Examples:

- season
- taste
- function
- weight
- volume
- techniques
- tips
- botanical_relatives
- flavors_to_avoid

## 7. `pairing_observations.csv`

One row per source observation. It preserves provenance and does not prematurely merge duplicate evidence.

`paired_entity_id` may be blank when a composite or ambiguous source phrase has not been normalized.

## 8. `affinity_groups.csv` and `affinity_members.csv`

Multi-ingredient combinations are not ordinary binary pairings. Preserve the original affinity text and parse members separately.

## 9. Derived aggregate table

The application may generate a `pairing_edges` table from approved observations:

```text
entity_a_id
entity_b_id
source_count
observation_count
max_supported_strength
weighted_score
```

This table is generated output, not manually authored source data.
