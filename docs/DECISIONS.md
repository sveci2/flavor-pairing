# Decisions

Resolutions to open design questions identified during repository analysis, before the
data-foundation implementation begins. Each entry records the question, the decision, and the
reasoning, so future changes can be checked against *why* a choice was made, not just *what* was
chosen.

---

## A. Granularity of `entity_source_names.csv`

**Question:** Does a row represent a unique source-string-to-entity mapping, or one row per
occurrence in the raw data?

**Decision:** `entity_source_names.csv` holds **unique mappings only**, keyed on
`(source_id, source_text, source_role)`. A given surface string (e.g. `APPLE` as a `subject` in
`src_synthetic_main_pairing`) is mapped to a canonical entity exactly once, regardless of how many
raw rows contain it.

Per-occurrence detail is not lost — it already lives in `pairing_observations.csv` and
`parsed_source_rows.csv`, both of which carry `source_record_id` back to the specific raw row.
`entity_source_names.csv` answers "what does this string mean," not "where did it appear";
occurrence-level questions are answered by joining through `source_record_id`.

**Consequence:** the current `data/sample/entity_source_names.csv` has duplicate rows for the same
`(source_id, source_text, source_role)` tuple (e.g. `APPLE`/`subject` mapped six times). This is a
sample-data defect to fix during implementation, not a schema change — no column changes are
needed, only deduplication of sample rows and a uniqueness check in validation.

---

## B. Should an affinity's subject also appear as an affinity member?

**Question:** In `data/sample/affinity_groups.csv`, `subject_entity_id` (e.g. `ent_0001` apple) is
also listed as `member_order = 1` in `affinity_members.csv`. Is that intentional?

**Decision:** Preserve the original source text in `affinity_text_raw` exactly as it appeared
(e.g. `apple + cinnamon + walnut`), including the subject if the source phrase included it. For
`affinity_members.csv`, the normalized rule is:

- **Every token that appears in `affinity_text_raw`, split according to the reviewed
  normalization rule for that source format, becomes a member row** — including the subject, if
  the subject's own name is one of the tokens in the raw phrase.
- The subject is **not synthetically added** as a member if it does not literally appear as a
  token in `affinity_text_raw`. Membership always reflects the source phrase, never an inference.

This keeps `affinity_members.csv` a faithful parse of `affinity_text_raw` rather than a
half-derived, half-inferred list. It also means `subject_entity_id` and a `member_entity_id` may
legitimately coincide, and validation must treat that as an expected case, not an error.

---

## C. Representing and testing non-ingredient entity types

**Question:** `entity_type` must support `ingredient`, `cuisine`, `dish`, `beverage`,
`technique`, `preparation`, `category`, and `unknown`, but current sample data is 100%
`ingredient`.

**Decision:**
- No schema change is needed — `entities.csv` already supports the full `entity_type` enum.
- Non-ingredient entities are **never derived automatically from attribute values** or other
  indirect context — promoting an attribute value (e.g. `bake`) to a `technique` entity is
  inference, not observation, and the governance docs prohibit inventing facts the source does
  not directly assert.
- Non-ingredient entities are created only from **explicit source structure** (a cuisine, dish,
  technique, or category appearing directly as a subject or pairing entry) or from **reviewed
  human decisions** recorded in the decision tables (see §J).
- To exercise these paths with test data, the *synthetic* sample source (project-owned,
  `src_synthetic_*`) will be extended with explicit raw rows containing non-ingredient entries
  (e.g. a cuisine or dish appearing as a pairing entry). This is legitimate because that source
  is demonstration material, not culinary evidence.
- Automated tests must assert that the parser and normalizer do not default an ambiguous or
  unrecognized source phrase to `entity_type = ingredient`; the default for anything not
  positively identified is `unknown`, subject to review.

---

## D. Validating `parent_entity_id`

**Question:** No sample row uses `parent_entity_id`; there is no defined validation for it.

**Decision:** `parent_entity_id`, when present, must:
1. Reference an existing `entity_id` in `entities.csv` (referential integrity, enforced by
   validation the same way `paired_entity_id` and `member_entity_id` already are).
2. Not create a cycle (an entity may not be its own ancestor through any chain of
   `parent_entity_id` references). Validation performs a cycle check over the parent graph.
3. Not be required — most entities will have no parent. Hierarchy is opt-in and only recorded
   when the source or a reviewed normalization decision actually establishes one (e.g. a
   preparation that is a specific form of a broader category). It is never inferred silently by
   the importer.

Test coverage will include: a valid parent chain, a self-reference (rejected), and a cycle across
two or more entities (rejected).

---

## E. `import_mappings.csv` as the actual source of truth

**Question:** `scripts/import_to_raw.py` currently hard-codes a Python `COLUMN_MAPS` dict that
duplicates what `data/*/import_mappings.csv` declares. If the CSV is edited, the script does not
change behavior.

**Decision:** The importer will **load `import_mappings.csv` at runtime** and derive its column
mapping from it — no mapping logic will be duplicated in Python. Concretely:

- For a given `source_format`, the importer reads all `import_mappings.csv` rows where
  `source_format` matches, and for each `target_field` in `{subject_raw, entry_raw, quality_raw}`
  reads the corresponding `input_column` (or treats it as "leave blank" when
  `input_column = "(not present)"`).
- `required = 1` rows are enforced: import fails fast (with a clear error identifying the source
  row) if a required input column is missing from the actual source file's header.
- Adding support for a new **flat tabular** source format becomes a **data change** (a new set of
  rows in `import_mappings.csv`), not a code change; non-tabular formats go through the narrow
  source-adapter interface instead (see §I). `COLUMN_MAPS` is removed from
  `scripts/import_to_raw.py` entirely once the loader is in place.
- A test asserts that every `source_format` value appearing in `sources.csv` has a corresponding
  complete set of rows in `import_mappings.csv`, so the two files cannot silently drift apart.

---

## F. Anti-fabrication rule keyed on format, not on a hard-coded source ID

**Question:** `scripts/validate_sample.py` currently checks
`rr["source_id"] == "src_synthetic_main_pairing"` before applying the "no invented score on
unmarked lowercase text" rule. This will not generalize to new sources sharing the same format.

**Decision:** The rule is re-keyed on **`source_format`**, resolved via `sources.csv`
(`source_id → source_format`) joined against `strength_mappings.csv`
(`input_source_format → source_value_or_marker → normalized_score`), not on a literal source ID:

- For any raw row whose source's `source_format` has a `strength_mappings.csv` entry stating that
  ordinary/unmarked/lowercase text maps to a blank `normalized_score` (as `main_pairing_csv`
  already does), validation rejects any parsed/observed row for that source that carries a
  non-null `strength_score` without a marker justifying it.
- This makes the rule apply automatically to *any* source registered with `source_format =
  main_pairing_csv` (synthetic or real), and to any future format that declares the same
  "unmarked text → no score" policy in `strength_mappings.csv`, without editing validation code.
- `scripts/validate_sample.py` (or its replacement test suite) is updated to remove the hard-coded
  `source_id` string and perform this join instead.

---

## G. SQLite for import runs and normalized records

**Question:** Should the pipeline use SQLite as its working store for import runs and normalized
records, while still supporting CSV I/O?

**Decision:** **Yes, adopt SQLite as the internal store for the data-foundation phase**, with CSV
treated as the durable interchange/export format, not replaced by it:

- Each import run writes into a local SQLite database (gitignored, e.g. under
  `data/build/` or similar — never committed) whose tables mirror the CSV schema in
  `docs/SCHEMA.md` one-to-one.
- CSV remains the **source of truth for samples, templates, and any checked-in data** — SQLite is
  a derived, disposable working store that must always be exactly regenerable from
  `raw_source_rows.csv` plus mapping/review decisions.
- The importer/parser/normalizer therefore need an **export** step (SQLite → CSV) and an
  **import** step (CSV → SQLite) so either representation can be treated as canonical for a given
  operation: CSV for review/diffing/version control, SQLite for referential-integrity checks,
  joins, and idempotent upserts during a run.
- Rationale: hand-rolled CSV-only joins (as in `scripts/validate_sample.py` today) get
  increasingly expensive and error-prone as row counts grow into the hundreds or thousands that
  `AGENTS.md`/`CLAUDE.md` require the pipeline to support; SQLite gives real foreign keys, unique
  constraints, and transactional idempotent upserts (see `docs/DATA_FOUNDATION_PLAN.md`,
  Idempotency Strategy) without introducing an external database dependency (SQLite ships with
  Python's standard library).
- No SQLite file is ever committed to the repository; `.gitignore` covers the working database
  path (`data/build/`).
- **Amendment (CP0):** SQLite is disposable *except* that run metadata (`import_runs`,
  `run_rows`) is backed by a durable **append-only ingest ledger** persisted as CSV — versioned
  under `data/ledger/<source_id>/` for public/project-owned sources, and under the gitignored
  `data/imports_private/ledger/<source_id>/` for rights-restricted sources (see §H and
  `docs/DATA_FOUNDATION_PLAN.md` §3). Human review decisions live in checked-in **decision
  tables** and are *inputs to* — never outputs of — regeneration (see §J). Deleting the working
  database therefore loses nothing: it is rebuilt from raw CSVs + ledger + decision tables.
- SQLite is used in a PostgreSQL-portable way: portable SQL only in `schema.sql`, all
  SQLite-specific behavior isolated in `store/db.py`, and no reliance on SQLite `rowid`
  semantics (`docs/DATA_FOUNDATION_PLAN.md` §14).

---

## H. Source-record identity, run membership, and changed source files

**Question:** The original plan derived `source_record_id` from a running index over the input
file's order. Index-derived IDs are only stable for byte-identical files: inserting, removing, or
reordering a row would shift every subsequent row's identity, corrupting provenance downstream.
Separately, the plan did not define what happens when a row is *removed* from a source file.

**Decision:**

- **Content-derived identity.** `source_record_id = <source_id>:<sha256_16>:<occurrence_index>`,
  where `sha256_16` is the first 16 hex characters of SHA-256 over the canonical JSON array
  `[subject_raw, entry_raw, quality_raw, raw_payload_json]` (exact bytes as received), and
  `occurrence_index` is a 1-based counter over identical-content rows within a single file, in
  file order. Rows unchanged between file versions keep their IDs regardless of position; an
  edited row is a new record (new content = new observation). Occurrence-index reassignment among
  byte-identical duplicate rows across versions is harmless because the referenced content is
  identical by definition.
- **Run membership.** Each import run records its complete row membership and per-version
  ordering in `run_rows` (`run_id, source_record_id, source_order`). Per-version row order lives
  there, not in the immutable raw table; `raw_source_rows.source_order` is retained as "order at
  first ingestion," informational only.
- **Current version.** The latest completed run for a source defines its current version. Rows
  present in raw but absent from the latest run are *removed rows*: permanently preserved in
  `raw_source_rows` (historical observations are never deleted), but excluded from current
  parse/normalize output.
- **Durable ledger.** `import_runs` and `run_rows` are persisted as append-only CSV ledgers that
  survive deletion of the working SQLite database — versioned paths for public/project-owned
  sources, the gitignored private path for restricted sources (see §G amendment).
- Ingestion remains strictly append-only against raw data; re-ingesting byte-identical input
  (detected via a stored `input_file_hash`) is a no-op apart from the run record itself.

---

## I. Import mappings vs source adapters

**Question:** Can every future source format realistically be supported purely through
`import_mappings.csv` rows, with no code changes?

**Decision:** No — and the plan must not overpromise it. The two concepts are separated:

- **Flat tabular formats** (column-per-field CSVs and equivalents) are fully supported by
  `import_mappings.csv` alone: adding one requires only new config rows plus a `sources.csv`
  entry, with no code change. This remains an acceptance criterion, scoped to tabular sources.
- **Non-tabular formats** (e.g. a structured EPUB extraction, nested JSON, multi-row records)
  require a registered **source adapter** with a deliberately narrow contract: an adapter's only
  permitted output is immutable raw rows (`subject_raw`, `entry_raw`, `quality_raw`,
  `raw_payload_json`). An adapter may **not** parse, normalize, classify, or score — those remain
  the exclusive job of the shared parser/normalizer layers driven by the config tables.
- Every adapter-backed source is still registered in `sources.csv` and documented in
  `import_mappings.csv` (naming its adapter), so the config remains the complete registry of how
  every source enters the system.
- No adapter is implemented during the data-foundation phase; only the interface and its
  constraints are defined. Adapters are written when (and only when) a non-tabular source is
  approved for use.

---

## J. Decision tables vs derived tables; review durability

**Question:** If SQLite is disposable and normalized tables are regenerable from raw data, where
do human review decisions live, and how do they survive regeneration?

**Decision:** Tables are explicitly partitioned (see `docs/DATA_FOUNDATION_PLAN.md` §13):

- **Decision tables** — durable, versioned, human-owned *inputs* to every regeneration:
  `sources.csv`, `import_mappings.csv`, `strength_mappings.csv`, `attribute_labels.csv`,
  `affinity_split_rules.csv`, `entities.csv`, `entity_source_names.csv`.
- **Derived tables** — rebuilt on every run, never hand-edited: `parsed_source_rows`,
  `entity_attributes`, `pairing_observations`, `affinity_groups`, `affinity_members`.
- **Ledger** — append-only operational record: `import_runs`, `run_rows` (§H).
- **Merge rule:** a pipeline run may *add* rows to decision tables (e.g. newly discovered
  unresolved mappings) and update fields on rows carrying machine statuses
  (`normalization_status = unresolved` / `auto_mapped`, `review_status = needs_review`), but must
  **never overwrite any field on a row whose status records a human decision**
  (`review_status = approved`/`rejected`, `normalization_status = human_mapped`).
- **Review workflow:** the review queue is a report over existing tables; a human resolves an
  item by editing the relevant decision table (directly or via a `resolve` CLI helper), the edit
  is versioned with the repository, rerunning `normalize` applies it under the merge rule, and
  the queue shrinks because the resolved rows no longer match the needs-review predicates
  (`docs/DATA_FOUNDATION_PLAN.md` §7).
- A dedicated test (`test_review_durability.py`) runs the pipeline, applies a simulated human
  resolution, reruns, and asserts the resolution survives untouched.
