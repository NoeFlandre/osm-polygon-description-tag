# Dataset package

## Purpose

Provide the canonical boundary for constructing and validating dataset artifacts.

## Responsibilities

Own the versioned Arrow and GeoParquet schema, record transformation, atomic storage, manifests,
statistics, and deterministic dataset-card generation.

## Non-responsibilities

This package does not discover PBFs, control publication, or coordinate the end-to-end workflow.

## Public API

Import stable APIs from `osm_polygon_description_tag.dataset`, for example `SCHEMA`,
`transform_record`, `write_geoparquet`, `Manifest`, `collect_stats`, and
`generate_dataset_docs`. Legacy top-level modules re-export the same objects.

## Allowed dependencies

The `runtime` package, OSM data types, PyArrow, Shapely, PyProj, DuckDB, and Python's standard
library.

## Data flow and side effects

`transform_record` converts an exported OSM record into a schema row. `write_geoparquet` streams
bounded batches through owned temporary files and atomically promotes only a validated artifact.
Manifests identify source and output bytes; reporting derives statistics and documentation from
final artifacts and their matching manifests.

## Safety and determinism invariants

Every original OSM tag remains in `tags`; base and localized descriptions and names are also
projected into dedicated columns. Geometry remains Polygon or MultiPolygon WKB, with a strictly
positive WGS84 geodesic `area_m2`. GeoParquet writing and validation are bounded and atomic.
Manifest identity, statistics, and generated dataset-card content are deterministic and
artifact-derived.

## Tests

Run `uv run pytest tests/unit/dataset tests/contracts/test_schema_contract.py
tests/contracts/test_dataset_card.py tests/contracts/test_import_compatibility.py -q`.
Contract and integration tests additionally cover artifact schemas and repeatable output.

The H3 density map produced by `osm_polygon_description_tag.dataset.geography`
is a derived publication artifact, not a Parquet field. It is generated
alongside `stats.json` and the dataset card on every `generate_dataset_docs`
invocation.
