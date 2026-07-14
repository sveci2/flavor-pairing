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

## 10. Configuration tables (decision tables)

Durable, versioned, human-owned inputs to the pipeline. A run may append machine-proposed rows
but must never overwrite a human-reviewed row (see `docs/DECISIONS.md` §J).

### `import_mappings.csv`

Declares, per flat tabular `source_format`, which input column feeds which raw field
(`subject_raw`, `entry_raw`, `quality_raw`) and whether it is required. The importer reads this
at runtime; no column mapping is duplicated in code. Non-tabular formats are handled by a narrow
source adapter that may only emit raw rows (`docs/DECISIONS.md` §I).

### `strength_mappings.csv`

Declares how strength evidence normalizes to a label and 1–4 score, keyed on
`(input_source_format, marker_key)`:

- `marker_key` — machine-readable key matched exactly by the resolver, from a closed set:
  `explicit_label:<value>`, `asterisk_uppercase`, `uppercase`, `plain`.
- `source_value_or_marker` — human-readable description, documentation only, never parsed.
- A `plain` row with a blank `normalized_score` declares that ordinary/lowercase text in that
  format carries no reliable strength evidence; the resolver returns no score and nothing may
  substitute a default.

### `attribute_labels.csv`

One row per recognized attribute-line label per source format:

- `source_format`
- `source_label` — the label text as it appears in the source (e.g. `Season`), matched
  case-insensitively before a colon
- `attribute_name` — the normalized attribute key (e.g. `season`)
- `notes`

A `Label: value` line whose label is not registered here is not guessed to be an attribute — it
remains `unclassified` for review.

### `affinity_split_rules.csv`

One row per source format registering how affinities are recognized and split:

- `source_format`
- `affinity_header_phrase` — the header text that introduces affinity groups
  (e.g. `Flavor Affinities`)
- `member_delimiter` — the single reviewed delimiter used to split members (e.g. ` + `)
- `review_status` — only `approved` rules may be used for splitting
- `notes`

No other delimiter (comma, colon, `e.g.`, `esp.`) may ever be used to split affinity text.

## 11. Run ledger (operational, append-only)

Not source data and not a decision table: an append-only record of ingestion history that must
survive deletion of any working database.

- `import_runs` — `run_id, source_id, started_at, finished_at, input_file_hash, row_count,
  status`. One row per import run.
- `run_rows` — `run_id, source_record_id, source_order`. Complete row membership and per-version
  ordering for each run. The latest completed run defines a source's current version; raw rows
  absent from it are historically preserved but excluded from current normalized output.

Ledger location: `data/ledger/<source_id>/` (versioned) for public/project-owned sources;
`data/imports_private/ledger/<source_id>/` (gitignored) for rights-restricted sources.

`source_record_id` is content-derived (`docs/DECISIONS.md` §H), never positional:
`<source_id>:<sha256_16>:<occurrence_index>`.
