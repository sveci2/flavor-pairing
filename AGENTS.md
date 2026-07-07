# Instructions for Fable and other coding agents

- Build the importer around `data/templates/raw_source_rows.csv`.
- Preserve every raw source row unchanged.
- Never require enrichment fields that are absent from the source.
- Do not guess missing strength tiers.
- Ordinary lowercase text in a typography-lossy extraction must have a null strength score.
- Do not split composite phrases on commas, colons, `e.g.`, or `esp.` without a reviewed normalization rule.
- Treat cuisines, dishes, techniques, preparations, and categories as entities or unresolved source text, not automatically as ingredients.
- Keep flavor affinities as multi-member groups.
- Allow unresolved mappings and expose them in an admin review queue.
- Every normalized fact must retain `source_id` and `source_record_id`.
- Derived pairing scores must be reproducible and must not overwrite source observations.
- Do not include external datasets unless provenance and usage rights are recorded.
