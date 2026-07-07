# Data Foundation Implementation Plan

Status: **planning only — no importer code has been implemented yet.** This is the CP0-revised
plan, incorporating the blocking changes from the design review (record identity, run membership,
durable review decisions, marker keys, parser configuration, and the mappings-vs-adapters split).
It must be read together with `CLAUDE.md`, `docs/SCHEMA.md`, and `docs/DECISIONS.md` (especially
entries H–J), which this plan assumes and does not repeat.

## 1. Implementation language and dependencies

- **Language:** Python 3 (matches the existing `scripts/*.py`, no new language introduced).
- **Dependencies:** standard library only for the core pipeline —
  `csv`, `json`, `sqlite3`, `argparse`, `pathlib`, `dataclasses`, `hashlib`.
  No new runtime dependency is required to hit the goals in this plan.
- **Test dependency:** `pytest`, added as a development-only dependency (`requirements-dev.txt` or
  equivalent), replacing the current ad hoc `scripts/validate_sample.py` checks with a real test
  suite.
- No web framework, ORM, or external database server is introduced. SQLite (via `sqlite3`, stdlib)
  is the only storage engine, per `docs/DECISIONS.md` §G — and it is used in a
  PostgreSQL-portable way (§14) so a future migration is mechanical.

## 2. Repository and module structure

```
flavor_pairing/                  # new Python package (importable, testable)
    __init__.py
    config/
        __init__.py
        loaders.py              # load sources.csv, import_mappings.csv, strength_mappings.csv,
                                #   attribute_labels.csv, affinity_split_rules.csv
    ingest/
        __init__.py
        raw_ingest.py           # tabular CSV -> immutable raw rows, mapping-driven (§4, §10)
        adapters.py             # narrow source-adapter interface for non-tabular formats (§10);
                                #   interface definition only until a non-tabular source exists
    parse/
        __init__.py
        row_parser.py           # stateful parsing within ordered subject blocks (§5)
        typography.py           # typography detector emitting closed marker_key set (§11)
        strength.py             # marker_key -> (label, score, method) via strength_mappings.csv
    normalize/
        __init__.py
        entities.py             # parsed rows -> entities / entity_source_names
        attributes.py           # parsed rows -> entity_attributes
        observations.py         # parsed rows -> pairing_observations
        affinities.py           # parsed rows -> affinity_groups / affinity_members
    review/
        __init__.py
        queue.py                # review-queue report over existing tables (§7)
    dupes/
        __init__.py
        detect.py               # duplicate & reverse-pair detection (report-only, §8)
    store/
        __init__.py
        schema.sql              # portable SQL DDL mirroring docs/SCHEMA.md (§14)
        db.py                   # ALL SQLite-specific behavior isolated here (§14)
        csv_io.py               # CSV <-> SQLite import/export under the conventions in §14
    cli.py                       # single CLI entry point, subcommands (§17)

scripts/
    import_to_raw.py             # thin compatibility wrapper delegating to flavor_pairing.ingest
    validate_sample.py           # kept during transition; becomes a wrapper over validation module

tests/
    conftest.py
    test_config_loaders.py
    test_raw_ingest.py
    test_source_versions.py      # insert/remove/reorder/edit scenarios across file versions (§16)
    test_ledger.py               # ledger persistence survives working-DB deletion (§16)
    test_rights_enforcement.py   # restricted sources cannot write to committed paths (§16)
    test_row_parser.py           # incl. block-state transition cases (§16)
    test_typography.py           # marker_key detection per format (§16)
    test_strength_mapping.py
    test_entity_normalization.py # incl. non-ingredient entity rules (§16)
    test_review_durability.py    # review decisions survive regeneration (§16)
    test_affinity_handling.py
    test_duplicate_detection.py
    test_review_queue.py
    test_idempotency.py
    test_schema_validation.py
    test_csv_roundtrip.py        # conventions in §14, byte-level round-trip
    fixtures/
        main_pairing_sample.csv
        pair_quality_sample.csv

data/
    templates/                   # + attribute_labels.csv, affinity_split_rules.csv (added in CP0)
    sample/                      # + attribute_labels.csv, affinity_split_rules.csv (added in CP0)
    ledger/                      # NEW: durable append-only ingest ledger for public/project-owned
                                 #   sources; versioned/committed (§3)
    build/                       # NEW, gitignored: disposable SQLite working DB + run outputs
    imports_private/             # gitignored: restricted raw data AND restricted-source ledgers
```

## 3. Import-run model, record identity, and the ingest ledger

### Record identity (content-derived, not positional)

`source_record_id` is derived from row **content**, never from row position:

```
source_record_id = <source_id>:<sha256_16>:<occurrence_index>
```

- `sha256_16` — first 16 hex characters of SHA-256 over the canonical JSON array
  `[subject_raw, entry_raw, quality_raw, raw_payload_json]` (exact bytes as received; no trimming
  or case folding before hashing).
- `occurrence_index` — 1-based counter over identical-content rows within a single source file,
  in file order, so genuinely repeated identical lines each get a distinct ID. Because such rows
  are byte-identical, reassignment of occurrence indexes among them across file versions is
  harmless: an ID always refers to exactly the same content.

Consequences: rows unchanged between file versions keep their IDs regardless of position; an
edited row is a **new record** (new content = new observation), and the old record is preserved.
Inserting, removing, or reordering rows never shifts any other row's identity.

### Run membership

- An **import run** is the unit of work for ingesting one version of one source file. It is
  identified by a `run_id` (UTC timestamp + source_id, e.g.
  `20260706T120000Z_src_github_flavor_bible`).
- `import_runs` records: `run_id, source_id, started_at, finished_at, input_file_hash
  (sha256 of raw input bytes), row_count, status`.
- `run_rows` records the membership and ordering of every run:
  `run_id, source_record_id, source_order`. **Per-version row order lives here**, not in the
  immutable raw table. (`raw_source_rows.source_order` is retained as "order at first
  ingestion" — informational only; `run_rows` is authoritative for ordering within any given
  version.)
- **The latest completed run for a source defines the current version of that source.** Rows
  present in raw but absent from the latest completed run are *removed rows*: they remain
  permanently preserved in `raw_source_rows` (historical observations are never deleted), but
  they are **excluded from the current parse/normalize output**. Downstream layers operate on
  current-version membership, not on the full raw table.
- Ingestion is **append-only against raw data**: a run inserts raw rows only for content it has
  not seen before (new `source_record_id`s) and records full membership in `run_rows`.
  Re-ingesting byte-identical input (detected via `input_file_hash`) is a no-op apart from an
  `import_runs` entry recording that the run happened.

### Durable ingest ledger

`import_runs` and `run_rows` must survive deletion of the disposable working database (§13), so
they are persisted as an **append-only CSV ledger**, mirrored into SQLite at load time:

- Public/project-owned sources: `data/ledger/<source_id>/import_runs.csv` and
  `data/ledger/<source_id>/run_rows.csv` — versioned and committed.
- Rights-restricted sources: the same layout under
  `data/imports_private/ledger/<source_id>/` — gitignored, never committed, alongside the
  restricted raw data itself.
- Ledger files are append-only: rows are added at the end of a run and never edited or removed.

## 4. Immutable raw-ingestion process

- Input: an external file in a registered `source_format`, plus a `source_id` that must already
  exist in `sources.csv` with a recorded `rights_status`.
- **Rights enforcement:** ingestion refuses to run if `rights_status` for the given `source_id`
  is restricted/unverified and any output path (raw rows or ledger) falls inside a committed
  location. Restricted-source outputs may only be written under the gitignored private path.
  This is enforced in code and covered by a dedicated test (§16).
- For flat tabular sources, column mapping is resolved entirely from `import_mappings.csv`
  (§10) — no per-format Python branches. For non-tabular sources, a registered source adapter is
  used, constrained as described in §10.
- Every input row becomes exactly one `raw_source_rows` row: `subject_raw` and `entry_raw` copied
  verbatim (never trimmed/cased/rewritten), `quality_raw` copied verbatim or left blank,
  `raw_payload_json` capturing the full original row as a JSON object, so no original column is
  ever lost.
- Raw rows are **write-once**: the ingestion layer only performs `INSERT`, never `UPDATE` or
  `DELETE`, into the raw table; no CLI command exposes raw mutation, and a test asserts this.

## 5. Parser architecture (stateful within ordered subject blocks)

- Input: the **current version** of a source (raw rows in `run_rows` order for the latest
  completed run). Output: one `parsed_source_rows` row per current raw row (1:1).
- Rows are processed **in `source_order` within each subject block** (consecutive rows sharing
  the same `subject_raw`). Classification is not purely row-local: the parser carries small,
  deterministic per-block state — currently one flag, *affinity header seen for this subject* —
  because in Flavor-Bible-shaped sources, affinity groups are identifiable partly by following an
  affinity header. State resets at every subject-block boundary. All state transitions are
  enumerated and covered by tests (§16).
- The classifier pipeline, tried in order; each classifier either claims the row or passes it on:
  1. **Attribute-line detector** — recognizes `Label: value` shapes where `Label` matches a row
     in `attribute_labels.csv` for this source format (case-insensitive label match followed by a
     colon). Labels are config, not code (§ below).
  2. **Affinity-header detector** — recognizes the header phrase registered for this source
     format in `affinity_split_rules.csv` (e.g. `Flavor Affinities`). Sets the block state flag.
  3. **Affinity-group detector** — recognizes composite phrases containing the registered
     `member_delimiter` for this source format (e.g. ` + `), with the block-state flag as
     supporting evidence. Splitting happens **only** on the registered, reviewed delimiter —
     never speculatively on commas, colons, `e.g.`, or `esp.` (per `AGENTS.md`).
  4. **Pairing-candidate fallback** — subject/entry treated as a candidate binary pairing.
  5. Anything not claimed with sufficient confidence → `unclassified`, `requires_review = 1`.
- **Parser-rule configuration files** (added as templates in CP0, registered as decision tables
  in §13):
  - `attribute_labels.csv` — columns: `source_format, source_label, attribute_name, notes`.
    One row per recognized attribute label per source format; maps the source's label text
    (e.g. `Season`) to the normalized `attribute_name` (e.g. `season`). A `Label: value` line
    whose label is not registered here is **not** guessed to be an attribute — it falls through
    to `unclassified` for review.
  - `affinity_split_rules.csv` — columns: `source_format, affinity_header_phrase,
    member_delimiter, review_status, notes`. One row per source format; registers the affinity
    header phrase and the single reviewed member delimiter. Only rows with an approved
    `review_status` may be used for splitting.
- Strength resolution is a separate step from classification (§11). It never falls back to a
  default score.
- `parser_confidence` (`high`/`medium`/`low`) and `requires_review` are always set; nothing is
  parsed silently into a fact table without these markers.

## 6. Entity normalization architecture

- Input: current-version `parsed_source_rows` classified as `attribute`, `pairing_candidate`, or
  `affinity_group`.
- Each distinct source string resolves to:
  - an existing entity (by exact match — case-folded, whitespace-trimmed, nothing more
    aggressive — or by an existing reviewed alias in `entity_source_names.csv`), or
  - a newly created entity with a machine `normalization_status` (e.g. `auto_mapped`) and
    `review_status = needs_review`, or
  - **no entity at all** — the mapping stays unresolved (`entity_id` blank) and surfaces in the
    review queue.
- **Non-ingredient entity types** (per `docs/DECISIONS.md` §C, corrected):
  - Entities are **never derived automatically from attribute values** or other indirect context
    — promoting an attribute value (e.g. `bake`) to a `technique` entity is inference, not
    observation.
  - Non-ingredient entities are created only from **explicit source structure** (a
    cuisine/dish/technique appearing directly as a subject or pairing entry) or from **reviewed
    human decisions** recorded in the decision tables.
  - Any string whose type cannot be positively identified defaults to `entity_type = unknown`,
    subject to review. Nothing defaults to `ingredient` for convenience.
- `entity_source_names.csv` writes are idempotent unique upserts keyed on
  `(source_id, source_text, source_role)` (`docs/DECISIONS.md` §A), and the machine may never
  overwrite a human-reviewed row (§13).
- `parent_entity_id`, when set by a reviewed decision, is validated for referential integrity and
  acyclicity before being persisted (`docs/DECISIONS.md` §D).

## 7. Human review workflow

The review queue is a **report over existing tables** (nothing needing review is stored twice),
and resolution is a **decision-table edit** (nothing human-decided lives in a regenerable table):

1. **Discover:** `review-queue` (CLI, §17) lists outstanding items by table and reason —
   unresolved `entity_source_names` mappings, `parsed_source_rows` with
   `row_type = unclassified` or `requires_review = 1`, and any row with
   `review_status = needs_review`.
2. **Decide:** a human records the decision by editing the relevant **decision table** (§13) —
   e.g. adding/confirming a row in `entities.csv`, assigning `entity_id` in
   `entity_source_names.csv`, or registering a new label in `attribute_labels.csv` — and marks
   the row with a human-owned status: `review_status = approved` (or `rejected`), and for
   mappings `normalization_status = human_mapped`. Decision tables are versioned files, so every
   decision is durable and diffable. (A `resolve` CLI subcommand may assist with these edits, but
   the file is the record either way.)
3. **Apply:** rerun `normalize` (and `validate`). The normalizer consults decision tables first
   and obeys the merge rule (§13): it may add new machine rows and update machine-derived fields,
   but **never overwrites any field on a row carrying a human-owned status**.
4. **Shrink:** the review-queue report is recomputed from the data; resolved rows no longer match
   the needs-review predicates, so the queue shrinks exactly by what was decided.
5. Raw data is never touched by any part of this workflow.

Status vocabulary that marks human ownership (used by the merge rule and validation):
`review_status ∈ {needs_review, approved, rejected}`;
`normalization_status` machine values (`unresolved`, `auto_mapped`, …) vs the human value
(`human_mapped`). The machine may only modify rows carrying machine values.

## 8. Duplicate and reverse-pair detection

- **Repeated observations** — the same `(subject_entity_id, paired_entity_id)` asserted by
  multiple source rows, within or across sources — are expected evidence, not noise. Detection
  **flags and reports** them; it never merges or deletes, since `pairing_observations.csv` is
  deliberately one row per observation (`docs/SCHEMA.md` §7).
- **Reverse pairs** — `(A, B)` and `(B, A)` both observed — are likewise reported, never
  auto-merged: each direction may carry different strength evidence, and collapsing them would
  discard information the source asserted. Symmetric aggregation belongs to the derived
  `pairing_edges` layer (out of scope, §21).
- Detection keys are computed via ordered/unordered tuple hashing purely for lookup performance;
  they are not persisted as new source facts.
- The current data model assumes one observation per parsed row; validation asserts this 1:1
  invariant so the `(source_id, source_record_id)` upsert key stays sufficient.

## 9. Affinity-group handling

- Implements `docs/DECISIONS.md` §B: `affinity_text_raw` is preserved verbatim; members are
  derived strictly by splitting that text on the **registered, approved `member_delimiter`** from
  `affinity_split_rules.csv` for the source format — never a speculative split.
- `member_order` reflects token order in `affinity_text_raw`.
- Each member token goes through the same entity-normalization path as a pairing subject/entry
  (§6) — including staying unresolved when it can't be mapped.
- Affinity groups are never flattened into `pairing_observations` rows. Pairwise edges derived
  from affinity co-membership are a derived-layer concern, out of scope this phase (§21).

## 10. Configuration-driven source mappings and the source-adapter boundary

- **Flat tabular formats need no code changes.** `import_mappings.csv` is loaded at runtime by
  `config/loaders.py` into `{source_format: {target_field: (input_column_or_None,
  required_bool)}}`; adding a new flat tabular source format requires only new rows in
  `import_mappings.csv` (plus a `sources.csv` entry). `flavor_pairing/ingest/raw_ingest.py`
  contains no format-specific branching, and `import_to_raw.py`'s hard-coded `COLUMN_MAPS` is
  removed (`docs/DECISIONS.md` §E).
- **Non-tabular formats use a narrow source-adapter interface** (`docs/DECISIONS.md` §I):
  a registered adapter's *only* permitted output is immutable raw rows (`subject_raw`,
  `entry_raw`, `quality_raw`, `raw_payload_json`). An adapter may **not** parse, normalize,
  classify, score, or write to any later layer. Every adapter-backed source is still registered
  in `sources.csv` and documented in `import_mappings.csv` (with the adapter named in its rows),
  so the config remains the complete registry of how every source enters the system.
- A startup check fails fast if a `source_format` referenced by `sources.csv` has no
  corresponding `import_mappings.csv` rows, or if required target fields have no mapped input
  column (or registered adapter).

## 11. Configuration-driven strength mappings and marker keys

- A **typography detector** (`parse/typography.py`) maps each raw entry/quality value to a
  **closed set of machine-readable marker keys**:
  - `explicit_label:<value>` — an explicit quality label present in `quality_raw`
    (e.g. `explicit_label:heaven`);
  - `asterisk_uppercase` — leading asterisk plus fully uppercase entry text;
  - `uppercase` — fully uppercase entry text, no asterisk;
  - `plain` — ordinary/lowercase text (typography-lossy: proves nothing).
  The key set is closed and versioned with the code; extending it is a reviewed change.
- `strength_mappings.csv` carries a `marker_key` column (added in CP0) that the resolver matches
  **exactly**; the human-readable `source_value_or_marker` description is retained as
  documentation only and is never parsed by code.
- The resolver (`parse/strength.py`) is the only code reading this table; every scoring decision
  is auditable back to a config row. A format's `plain` row with blank `normalized_score` is what
  drives the anti-fabrication rule (`docs/DECISIONS.md` §F): the resolver returns no score and
  callers must not substitute a default.

## 12. Idempotency strategy

- **Raw layer:** content-derived `source_record_id`s (§3) mean re-ingesting byte-identical input
  produces zero new raw rows (detected up front via `input_file_hash`), and re-ingesting a
  changed file inserts only genuinely new content while `run_rows` records the new version's
  membership. Nothing is ever updated or deleted in raw.
- **Parse/normalize layers:** all writes are upserts keyed on natural keys:
  - `parsed_source_rows`: `(source_id, source_record_id)`;
  - `entity_source_names`: `(source_id, source_text, source_role)`;
  - `pairing_observations`: `(source_id, source_record_id)` (1:1 invariant, §8);
  - `affinity_groups`: `(source_id, source_record_id)`; `affinity_members`:
    `(affinity_id, member_order)`.
- Upserts respect the human-ownership merge rule (§13): a rerun may refresh machine-derived
  fields but never a human-decided one.
- SQLite `UNIQUE` constraints on these natural keys enforce idempotency at the database layer;
  upserts use `INSERT ... ON CONFLICT (...) DO UPDATE` (syntax shared with PostgreSQL, §14).
- The whole pipeline is rerunnable end-to-end from raw + decision tables at any time, producing
  identical output — verified by test (§16), not just asserted.

## 13. Decision tables vs derived tables

The partition that makes "regenerable from raw + mapping/review decisions" true:

- **Decision tables** — durable, versioned, human-owned **inputs** to every regeneration:
  - `sources.csv`
  - `import_mappings.csv`
  - `strength_mappings.csv`
  - `attribute_labels.csv`
  - `affinity_split_rules.csv`
  - `entities.csv`
  - `entity_source_names.csv`
- **Derived tables** — rebuilt on every run, never hand-edited:
  - `parsed_source_rows`
  - `entity_attributes`
  - `pairing_observations`
  - `affinity_groups`, `affinity_members`
- **Ledger** (append-only operational record, §3): `import_runs`, `run_rows`.
- **Merge rule:** regeneration may *add* rows to decision tables (e.g. newly discovered
  unresolved mappings appended to `entity_source_names.csv`) and update fields on rows carrying
  machine statuses, but must **never overwrite any field on a row whose status records a human
  decision** (§7). Human review decisions therefore survive every regeneration by construction.
- The SQLite working database (`data/build/`) is disposable: deleting it loses nothing, because
  decision tables, the ledger, and raw CSVs are the durable record from which it is rebuilt.

## 14. CSV and SQLite conventions; PostgreSQL portability

**CSV conventions** (apply to all checked-in and exported CSVs):

- **Encoding:** UTF-8 with BOM (`utf-8-sig`), matching the existing template files. Readers use
  `utf-8-sig` (tolerates BOM presence or absence); writers emit the BOM.
- **Null convention:** empty string in CSV ⇄ `NULL` in SQLite. The data model does not
  distinguish empty string from null; validation rejects any field that would require that
  distinction.
- **Column order:** exactly as declared in `data/templates/*.csv`; exports emit columns in
  template order, deterministically.
- **Row order:** exports are sorted by primary/natural key, deterministically, so repeated
  exports of identical data are byte-identical and diffs are meaningful.
- **Newlines:** LF (`\n`) line terminators on export; embedded newlines inside fields are
  preserved via standard CSV quoting (`csv` module defaults, `newline=""` on file handles).
- **Round-trip expectation:** CSV → SQLite → CSV is **byte-identical** for any file conforming to
  these conventions; the round-trip check (§15) enforces this on every run.

**PostgreSQL portability:**

- `store/schema.sql` uses portable SQL only: standard `TEXT`/`INTEGER` types, named
  `PRIMARY KEY`/`UNIQUE`/`FOREIGN KEY` constraints, `INSERT ... ON CONFLICT ... DO UPDATE`
  (shared by SQLite ≥3.24 and PostgreSQL).
- **No reliance on SQLite `rowid` semantics**, `AUTOINCREMENT`, or SQLite-only type affinity
  tricks; all keys are explicit application-supplied identifiers.
- All SQLite-specific behavior (connection setup, `PRAGMA foreign_keys = ON`, file paths) is
  isolated in `store/db.py`, so a future PostgreSQL migration touches one module plus the DDL
  file, nothing else.

## 15. Validation strategy

Three layers, all required to pass before any output is treated as usable:

1. **Structural/referential validation** (format-agnostic): every foreign key resolves or is
   legitimately blank; declared enums (`row_type`, `entity_type`, `strength_label`,
   `review_status`, `normalization_status`, marker keys) contain only documented values; required
   fields are non-blank; every current-version raw row has exactly one parsed row and vice versa;
   `entity_source_names` uniqueness on `(source_id, source_text, source_role)`; the 1:1
   observation-per-row invariant (§8).
2. **Policy validation** (the rules that make the data trustworthy): the anti-fabrication rule
   keyed on `source_format` + `marker_key` (`docs/DECISIONS.md` §F) — no score without a config
   row justifying it; affinity members must all be tokens of their group's `affinity_text_raw`
   under the registered delimiter; `parent_entity_id` referential integrity and acyclicity
   (`docs/DECISIONS.md` §D); every `sources.csv` `source_format` has complete
   `import_mappings.csv` / `strength_mappings.csv` / parser-rule coverage; no human-owned row
   altered by a machine run.
3. **Round-trip validation:** CSV export equals the SQLite working store content under the §14
   conventions, byte-identically, after any run.

## 16. Automated test plan

`pytest` suite under `tests/` (fixtures are dedicated small CSVs distinct from `data/sample/`):

- `test_config_loaders.py` — mappings/strength/parser-rule configs load correctly; startup
  coverage checks fail fast on incomplete config; only approved `affinity_split_rules` rows are
  usable.
- `test_raw_ingest.py` — column mapping from `import_mappings.csv` only; raw rows byte-verbatim;
  raw layer rejects update/delete; rerun on unchanged input is a no-op.
- `test_source_versions.py` — **insert, remove, reorder, and edit** a row across source file
  versions, re-ingest, and assert: no duplicated raw rows, no lost historical rows, correct
  `run_rows` membership, and correct **current-version** parse/normalize output (removed rows
  excluded, edited rows appear as new records).
- `test_ledger.py` — ledger CSVs are append-only and complete; deleting the working SQLite
  database and rebuilding from raw + ledger + decision tables reproduces identical state.
- `test_rights_enforcement.py` — ingestion of a restricted/unverified source refuses to write
  raw rows or ledger entries to any committed path.
- `test_row_parser.py` — each classifier on representative rows; unclassified fallback; no
  speculative splitting; **block-state transitions** (affinity header sets state; state resets at
  subject-block boundary; groups after headers classify correctly; delimiter-bearing rows without
  headers still classify with appropriate confidence).
- `test_typography.py` — the typography detector emits exactly the closed marker-key set;
  `explicit_label:<value>`, `asterisk_uppercase`, `uppercase`, `plain` each detected correctly
  per format; nothing outside the closed set is ever emitted.
- `test_strength_mapping.py` — every `strength_mappings.csv` row resolves by `marker_key`;
  `plain` resolves to no score for any format declaring that policy, generically — keyed on
  format, not on any `source_id` (`docs/DECISIONS.md` §F).
- `test_entity_normalization.py` — unique upsert for `entity_source_names` (§A); **non-ingredient
  entities** only from explicit source structure or human decision, never mined from attribute
  values; uncertain types default to `unknown`, never to `ingredient`; unresolved mappings keep
  blank `entity_id`.
- `test_review_durability.py` — run pipeline → apply a simulated human resolution to a decision
  table → rerun pipeline → the resolution survives untouched and the review queue shrinks
  accordingly.
- `test_affinity_handling.py` — members always tokens of `affinity_text_raw` under the registered
  delimiter (§B); affinity groups never produce pairwise observations.
- `test_duplicate_detection.py` — repeats and reverse pairs flagged, never merged.
- `test_review_queue.py` — every review-eligible row discoverable; resolution is a decision-table
  write, never a raw edit.
- `test_idempotency.py` — full pipeline twice on identical input: identical output, zero
  duplicates; rerun after a source correction changes only the delta.
- `test_schema_validation.py` — supersedes `scripts/validate_sample.py` (referential integrity,
  enums, acyclicity, config coverage), parametrized over `data/sample/` and fixtures — no
  hard-coded source IDs or row counts.
- `test_csv_roundtrip.py` — §14 conventions: BOM, null mapping, column/row order determinism,
  LF newlines, byte-identical round trip.
- CI: `.github/workflows/tests.yml` running `pytest` + schema validation on every push/PR
  (added at CP7).

## 17. CLI commands

Single entry point `python -m flavor_pairing.cli <command>`:

- `ingest --source-id ID --format FORMAT INPUT_CSV` — raw ingestion + ledger append (§3, §4).
- `parse --source-id ID` — parser over the source's current version (§5).
- `normalize --source-id ID` — normalization under the merge rule (§6, §9, §13).
- `review-queue [--table TABLE]` — outstanding review items (§7).
- `resolve …` — optional assistant for recording a decision-table edit (§7); the file is the
  record either way.
- `detect-duplicates --source-id ID` — duplicate/reverse-pair report (§8).
- `validate [--target csv|sqlite|both]` — all three validation layers (§15).
- `export --to csv OUT_DIR` / `import --from csv IN_DIR` — CSV ⇄ SQLite under §14 conventions.
- `run --source-id ID --format FORMAT INPUT_CSV` — chains ingest → parse → normalize → validate;
  **stops at the first failure and exits non-zero**, so it is CI-safe.

Every command is non-interactive.

## 18. Expected generated outputs

- A disposable SQLite working database under `data/build/` (gitignored) mirroring
  `docs/SCHEMA.md` plus `import_runs`/`run_rows`.
- Durable append-only ledger CSVs under `data/ledger/` (or the private path for restricted
  sources) (§3).
- Regenerated CSV exports matching template headers and §14 conventions, written to a
  run-specific directory under `data/build/` — never overwriting `data/sample/` automatically;
  promotion into `data/sample/` is a manual, reviewed step.
- Review-queue, duplicate/reverse-pair, and validation reports (human-readable text or CSV).
- `scripts/validate_sample.py` remains during transition as a thin wrapper over the validation
  module — one implementation of the rules, not two.

## 19. Acceptance criteria

The data-foundation phase is done when:

1. The `flavor_pairing` package exists per §2, covered by the §16 tests, all passing.
2. `COLUMN_MAPS` is gone; column mapping comes only from `import_mappings.csv`, verified by a
   test adding a new **flat tabular** format purely via CSV rows with no code change
   (`docs/DECISIONS.md` §E/§I). The source-adapter interface exists with its constraint (raw
   rows only) enforced by its type/contract and documented; no adapter implementation is required
   until a non-tabular source is approved.
3. The anti-fabrication rule is verified generically via `source_format` + `marker_key`
   (`docs/DECISIONS.md` §F), tested with a new `source_id` sharing an existing format.
4. Source-version handling is verified: insert/remove/reorder/edit scenarios (§16
   `test_source_versions.py`) pass; removed rows are preserved historically and excluded from
   current output; re-running on identical input is a no-op.
5. Review durability is verified: a human decision recorded in a decision table survives full
   regeneration (`test_review_durability.py`), and the ledger survives working-DB deletion
   (`test_ledger.py`).
6. Decisions A–D each have a passing test tied to the corresponding `docs/DECISIONS.md` entry
   (unique source-name mappings; affinity members from raw text; non-ingredient entity rules;
   `parent_entity_id` acyclicity).
7. CSV ⇄ SQLite round-trips byte-identically under §14 conventions for a full sample run.
8. Rights enforcement is verified: restricted sources cannot write to committed paths
   (`test_rights_enforcement.py`).
9. CI runs the test suite and validation on every push.
10. `data/sample/*.csv` is regenerated from a real pipeline run (not hand-authored), passes all
    three validation layers, and demonstrates every documented row type, entity type, and
    strength-resolution path.
11. No data or identifier from `data/imports_private/` appears anywhere in the repository; no
    external network fetch occurs anywhere in the codebase or CI.

## 20. Implementation checkpoints

Each checkpoint is independently shippable, ends with all existing tests green and
`scripts/validate_sample.py` passing, and gets its own commit(s) on `feature/data-foundation`.

- **CP0 — Plan revision (this document).** No code. Blocking review findings folded into the
  plan, `docs/DECISIONS.md` (entries H–J), `docs/SCHEMA.md`, and the config templates
  (`marker_key`, `attribute_labels.csv`, `affinity_split_rules.csv`).
- **CP1 — Package skeleton + config loaders.** `flavor_pairing/config/`, loaders for all five
  config tables, fail-fast coverage checks, pytest wired up (`test_config_loaders.py`).
- **CP2 — Store layer.** `schema.sql` (portable SQL), `db.py`, `csv_io.py`,
  `test_csv_roundtrip.py` green under §14 conventions.
- **CP3 — Raw ingestion + run model.** Content-hash record IDs, `import_runs`/`run_rows` ledger,
  rights enforcement, append-only raw layer; `test_raw_ingest.py`, `test_source_versions.py`,
  `test_ledger.py`, `test_rights_enforcement.py` green; `import_to_raw.py` becomes a wrapper and
  `COLUMN_MAPS` is deleted. (Riskiest checkpoint — lands the record-identity machinery.)
- **CP4 — Parser + strength resolution.** Block-stateful classifier chain, typography detector,
  marker-key resolver, policy validation keyed on format; `test_row_parser.py`,
  `test_typography.py`, `test_strength_mapping.py` green.
- **CP5 — Normalization + review workflow.** Entities/names/attributes/observations, merge rule,
  review queue + resolution path, sample `entity_source_names` dedup + uniqueness check;
  `test_entity_normalization.py`, `test_review_durability.py`, `test_review_queue.py`,
  `test_idempotency.py` green.
- **CP6 — Affinities + duplicate/reverse-pair reports.** `test_affinity_handling.py`,
  `test_duplicate_detection.py` green.
- **CP7 — Regeneration + CI.** `data/sample/` regenerated from a real run, `test_schema_validation.py`
  parametrized and green, `.github/workflows/tests.yml` added, acceptance-criteria sweep.

## 21. Explicit out-of-scope items (this phase)

- The consumer-facing application (search, recommendations, any UI).
- The derived `pairing_edges` aggregation table (`docs/SCHEMA.md` §9) — premature until a corpus
  of approved observations exists.
- Ingesting the real GitHub Flavor Bible CSV or Gist quality dataset — rights-unresolved per
  `docs/DATA_SOURCES.md`; nothing external may be fetched without explicit approval.
- Processing anything under `data/imports_private/` — rights-restricted, out of scope until
  separately authorized.
- Any concrete non-tabular source adapter implementation (the interface and its constraints are
  defined; adapters are written only when a non-tabular source is approved).
- A review-queue UI or any human-facing workflow beyond the CLI + decision-table edits.
- A LICENSE file or any licensing decision for this repository (per explicit instruction).
- Chemical/flavor-compound similarity, confidence scoring, or any other enrichment field listed
  as `not_in_core` in `docs/field_decisions.csv`.
- The PostgreSQL migration itself (portability is preserved, §14; migration is not performed).
- Performance/scale work beyond "hundreds or thousands of records."
