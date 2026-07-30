# Dataset package

## Purpose

Provide the canonical boundary for constructing and validating dataset artifacts.

## Responsibilities

The versioned Arrow and GeoParquet schema, record transformation, atomic storage, manifests,
statistics, and dataset-card generation belong here as they are migrated.

## Non-responsibilities

This package does not discover PBFs, control publication, or coordinate the end-to-end workflow.

## Public API

Exports are introduced incrementally during the package reorganization. This initial boundary has
no public exports.

## Allowed dependencies

The `runtime` package, OSM data types, PyArrow, Shapely, PyProj, DuckDB, and Python's standard
library.

## Data flow and side effects

Future dataset APIs will convert validated exported records into atomic GeoParquet and metadata
artifacts under approved paths.

## Safety and determinism invariants

Schema versions, ordering, geometry, tags, statistics, manifests, and generated documentation
remain deterministic and validated.

## Tests

Dataset unit tests cover schema, transformation, storage, manifests, and reporting; contract and
integration tests cover artifact schemas and repeatable output.
