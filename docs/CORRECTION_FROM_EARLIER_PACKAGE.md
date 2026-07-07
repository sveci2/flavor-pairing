# Correction to the earlier sample package

The earlier package mixed source data with invented product-enrichment fields. It should be treated only as UI test data.

## Do not use as required production fields

- category and subcategory
- ingredient form
- processing state
- vegan and vegetarian flags
- alcohol and allergen flags
- aroma and texture tags
- numerical taste profiles
- pairing type
- contexts and techniques for every pairing
- generated explanations
- confidence
- evidence count

These values may be useful in the finished application, but they must come from separate sources or transparent derivation.

## Source-compatible replacements

- fixed taste columns → `entity_attributes`
- one pairing score → raw quality/format marker plus nullable normalized score
- flat pair table → source observations
- comma-separated affinity text → affinity group and member tables
- assumed ingredient names → source-name mapping with review status
- hard-coded 100 rows → generic raw import pipeline
