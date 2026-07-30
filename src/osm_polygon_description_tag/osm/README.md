# OSM package

## Purpose

Provide the canonical boundary for deterministic OpenStreetMap source ingestion.

## Responsibilities

PBF discovery and bounded `osmium export` streaming belong here as they are migrated.

## Non-responsibilities

This package does not transform exported records, write GeoParquet, publish files, or manage
workflow state.

## Public API

Exports are introduced incrementally during the package reorganization. This initial boundary has
no public exports.

## Allowed dependencies

The `runtime` package and Python's standard library.

## Data flow and side effects

Future OSM APIs will discover source PBFs and stream exported records from bounded subprocesses;
they will not write final dataset artifacts.

## Safety and determinism invariants

Discovery order and source identities remain deterministic, and subprocess output remains bounded
and validated before use.

## Tests

OSM unit tests cover discovery and extraction streams; integration tests cover the external
`osmium` boundary, and contract tests preserve existing import paths.
