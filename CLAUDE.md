# CLAUDE.md

Instructions for Claude Code (and other coding agents) working in this repository.

Read `AGENTS.md`, `README.md`, `docs/SCHEMA.md`, `docs/CORRECTION_FROM_EARLIER_PACKAGE.md`,
`docs/field_decisions.csv`, `docs/DATA_SOURCES.md`, and `docs/DECISIONS.md` before changing
anything in this project. This file summarizes and preserves the rules already established in
those documents; it does not replace them, and it must not be edited to loosen a rule those
documents establish without the user's explicit approval.

## Non-negotiable rules

- **Raw source rows are immutable.** Once a row is written to `raw_source_rows.csv`, it is never
  edited, deleted, or rewritten. Corrections happen in later layers, never by mutating raw data.
- **Keep raw, parsed, normalized, enriched, and derived data separate.** Each layer lives in its
  own tables/files. Do not collapse layers together, and do not write parsing or normalization
  logic that overwrites an earlier layer in place.
- **Never invent missing information.** If a source does not contain a field, a score, or a
  category, leave it blank/null. Do not guess, interpolate, or default it to make output look
  more complete.
- **Strength scores remain null where the source does not preserve enough evidence.** In
  particular, ordinary lowercase text in a typography-lossy extraction must have a null
  `strength_score`. Do not convert unmarked/lowercase rows to a default score.
- **Ambiguous records go to review, not to a guess.** Unresolved entity mappings, unclassified
  parser rows, and uncertain normalization must remain visible (e.g. `needs_review`,
  `unclassified`, unresolved foreign keys) rather than being forced into a best-effort guess.
- **Multi-ingredient affinities are not binary pairings.** Preserve affinity text and membership
  in `affinity_groups.csv` / `affinity_members.csv`; do not decompose an affinity group into
  ordinary pairwise `pairing_observations.csv` rows.
- **Every normalized fact must retain source provenance.** Any row in a normalized or derived
  table must be traceable back to a `source_id` and `source_record_id` (directly, or transitively
  through a table that carries them).
- **Private or rights-restricted data must not be committed.** Nothing under `data/imports_private/`
  (or any other rights-restricted source) may be committed to this repository. Respect
  `docs/DATA_SOURCES.md` rights statuses.
- **No external data may be fetched without explicit approval.** Do not download, scrape, or
  otherwise pull in external datasets on your own initiative. If a task seems to require it, ask
  first.
- **Do not hard-code sample entities, source IDs, or row counts.** Importers, parsers, and
  validators must work generically for any number of records and any registered source, not just
  the current synthetic samples (`src_synthetic_main_pairing`, `src_synthetic_quality`, etc.).
- **Human review decisions must survive regeneration.** Review decisions live in durable,
  versioned decision tables (see `docs/DECISIONS.md` §J); automated runs may append
  machine-proposed rows but must never overwrite any field on a row whose status records a human
  decision. Never programmatically edit a human-reviewed row.

## Working rules

- Make changes on a separate branch. Do not merge into `main`.
- Add or update automated tests for material changes.
- Run validation and tests before claiming completion, and report actual results.
- Keep the first development phase focused on the data foundation, not the consumer-facing
  application.
- Do not add a LICENSE file unless the user explicitly asks for one; this is currently a private
  repository with no software or data licence selected.

## Where the underlying rules live

- `AGENTS.md` — importer/agent-specific build constraints.
- `README.md` — the three-layer data package rationale (raw / parsed-normalized / derived).
- `docs/SCHEMA.md` — the data contract: table-by-table field definitions.
- `docs/CORRECTION_FROM_EARLIER_PACKAGE.md` — fields that were removed from the required core and
  why; source-compatible replacements.
- `docs/field_decisions.csv` — per-field status (`required_source`, `derived_nullable`,
  `not_in_core`, etc.) and rationale.
- `docs/DATA_SOURCES.md` — provenance and rights registry for every dataset considered.
- `docs/DECISIONS.md` — resolutions to open design questions raised during planning.
- `docs/DATA_FOUNDATION_PLAN.md` — the implementation plan for the data-foundation phase.
