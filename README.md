# Source-compatible flavour-pairing data package

This replaces the earlier synthetic 100-ingredient model as the recommended input contract for Fable.

## Core rule

Never require information that the source does not contain.

The package uses three layers:

1. **Raw source layer** — stores each source row exactly as received.
2. **Parsed/normalized layer** — classifies rows and maps names, but allows nulls and review flags.
3. **Derived product layer** — scoring, aggregated edges, explanations, filters, and enrichment are built later from documented evidence.

## Compatible source shapes

### Two-column source

```csv
main,pairing
APPLE,cinnamon
APPLE,Season: autumn
APPLE,Flavor Affinities
APPLE,apple + cinnamon + walnut
```

Mapping:

- `main` → `subject_raw`
- `pairing` → `entry_raw`
- `quality_raw` → blank

### Three-column source

```csv
ingredient1,ingredient2,quality
chard,anchovy,heaven
```

Mapping:

- `ingredient1` → `subject_raw`
- `ingredient2` → `entry_raw`
- `quality` → `quality_raw`

## Why raw and normalized data are separate

A source entry may be:

- an ingredient pairing;
- a cuisine;
- a dish or preparation;
- an attribute such as season or taste;
- a tip;
- an affinity header;
- a multi-ingredient affinity;
- a composite entry such as several cheeses in one cell.

The importer must not destructively force all of these into a single ingredient-pair table.

## Fields deliberately removed from the required core

The following are not required because the referenced pairing sources do not consistently provide them:

- vegan or vegetarian status;
- allergens;
- numerical sweetness, bitterness, or aroma scores;
- ingredient form and processing state;
- pairing explanations;
- context and technique for every pair;
- confidence and evidence count;
- chemical similarity.

They can be added later through separate, cited enrichment sources.

## Strength warning

The two-column GitHub-style extraction preserves uppercase and some asterisks, but ordinary lowercase text does not prove whether the original item was plain or bold. The schema therefore leaves `strength_score` blank when the source does not preserve enough formatting.

Do not convert every lowercase row to score 1.

## Fable build requirement

Fable must build imports against `raw_source_rows.csv`, not against a fixed list of 100 ingredients. All derived tables must be regenerable from raw rows and mapping/review decisions.
