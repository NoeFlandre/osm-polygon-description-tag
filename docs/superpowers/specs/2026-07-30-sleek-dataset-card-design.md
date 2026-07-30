# Sleek Public Dataset Card Design

## Goal

Replace the oversized generated dataset card with a concise, public-facing
overview that answers the questions a prospective user cares about first,
while preserving complete machine-readable provenance in `stats.json`.

## Boundaries

- The Parquet schema, geometry, area calculation, manifests, publication
  plans, and resumability semantics do not change.
- Statistics remain deterministic functions of validated Parquets and matching
  manifests.
- No wall-clock value enters generated artifacts.
- Detailed per-file rows and SHA-256 identities remain in `stats.json` but are
  not rendered into the dataset card.
- The implementation and tests use synthetic artifacts only. They do not read
  the real PBF corpus, mutate the Seagate data root, or contact Hugging Face.

## Word-count contract

Base and localized descriptions are reported separately.

- A base description value is the non-null `description` field derived from
  the plain `description=*` tag.
- A localized description value is each entry in
  `localized_descriptions`, derived from `description:<suffix>=*`.
- If one feature has multiple localized description tags, each non-empty map
  value is one localized description value.
- Word counts use deterministic Unicode-aware whitespace tokenization:
  normalize no text, split the exact stored value on Unicode whitespace, and
  count non-empty tokens.
- Each category reports description-value count, total words, and median words
  per description value.
- Empty categories report zero values, zero total words, and a null median.
- Existing exact suffix frequencies remain machine-readable. The card renders
  only the ten most frequent localized suffixes, ordered by descending count
  and then exact suffix ascending for ties. Suffixes are explicitly described
  as unvalidated tag suffixes, not guaranteed language codes.

The statistics schema version increases because `stats.json` gains:

- `base_description_values`
- `base_description_words_total`
- `base_description_words_median`
- `localized_description_values`
- `localized_description_words_total`
- `localized_description_words_median`

## Dataset-card structure

The generated README is intentionally compact:

1. A one-paragraph dataset summary.
2. A generated “Dataset at a glance” table containing total polygons,
   Parquet files, total output size, ways, relations, Polygons, and
   MultiPolygons.
3. A generated “Description coverage” table with separate base and localized
   value counts, total words, and median words.
4. A generated top-ten localized-suffix table.
5. A short schema section grouping columns by purpose rather than a long
   row-per-column table.
6. Minimal PyArrow and GeoPandas loading examples.
7. Concise methodology, limitations, attribution/license, and reproducibility
   sections.

The generated block may include the dataset timestamp range and a compact area
summary when available. It does not render emitted-feature totals,
transformation rejection tables, full suffix tables, per-file inventories,
source/output byte pairs, or checksums. Those facts remain available in
`stats.json` and manifests.

## Determinism and resumability

`collect_stats` continues to validate every Parquet/manifest pair before
aggregation. Word statistics are computed with bounded DuckDB aggregation and
do not materialize the dataset in Python.

`generate_dataset_docs` continues to use write-if-changed atomic replacement.
Given identical validated artifacts and template bytes, `stats.json` and
README bytes and mtimes remain unchanged. A changed template or statistics
payload changes the metadata upload identity, so the existing independently
resumable metadata step republishes only `README.md` and `stats.json`.

## Testing

RED/GREEN tests prove:

- base and localized values and word totals are separate;
- multiple localized values on one feature are counted independently;
- Unicode whitespace produces deterministic token counts;
- medians are correct for odd, even, and empty categories;
- top suffix rendering is deterministically limited to ten;
- detailed per-file rows and SHA-256 values remain in `stats.json` but not the
  README;
- the README contains the concise public metrics and loading guidance;
- regeneration remains byte- and mtime-stable;
- metadata-only resumability remains valid;
- the full offline quality, integration, coverage, wheel, and CLI gates pass.

## Safe operational handoff

The currently running process must not be modified or restarted. Code is
implemented and pushed without changing its checked-out `HEAD`. After that
process exits, the operator may fast-forward the local checkout and rerun the
single `just run-and-publish` command. Existing validated Parquets and
publication state are reused; deterministic metadata is regenerated and
published independently.
