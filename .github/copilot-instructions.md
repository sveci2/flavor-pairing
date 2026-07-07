# Flavour Pairing Repository Instructions

Read `AGENTS.md`, `README.md`, `docs/SCHEMA.md`, `docs/CORRECTION_FROM_EARLIER_PACKAGE.md`, and `docs/field_decisions.csv` before changing the project.

## Core principles

* Preserve original source data without rewriting or deleting it.
* Never invent fields or values that a source does not contain.
* Keep raw source records separate from parsed, normalized, enriched, and derived data.
* Every normalized record must remain traceable to its source and source record.
* Allow ambiguous and unresolved records instead of guessing.
* Do not assume every source entry is an ingredient.
* Preserve multi-ingredient flavour affinities separately from binary pairings.
* Do not assign pairing-strength scores where source formatting or quality information has been lost.
* Do not add external datasets unless their source and rights status have been documented and approved.
* Do not commit EPUBs, PDFs, private imports, credentials, API keys, tokens, or `.env` files.
* Do not hard-code the current sample ingredients or number of records.
* Build importers that can support hundreds or thousands of records using the same schema.

## Working rules

* Make changes on a separate branch.
* Do not merge changes into `main`.
* Add or update automated tests for material changes.
* Run validation and tests before claiming completion.
* Report actual test results.
* Keep the first development phase focused on the data foundation, not the consumer-facing application.
