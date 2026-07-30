# Runtime package

## Purpose

Provide the canonical boundary for process-wide runtime support.

## Responsibilities

Approved path configuration, packaged-resource lookup, operational logging, and safe cleanup of
abandoned temporary files belong here as they are migrated.

## Non-responsibilities

This package does not discover OSM inputs, build dataset records, publish artifacts, or coordinate
the workflow.

## Public API

Exports are introduced incrementally during the package reorganization. This initial boundary has
no public exports.

## Allowed dependencies

Python's standard library and static package resources.

## Data flow and side effects

Future runtime APIs may read configuration and packaged resources, write operational logs, and
remove only verified abandoned temporary files owned by this application.

## Safety and determinism invariants

Paths remain explicitly validated, packaged resources are immutable, and cleanup stays confined to
identified application-owned files.

## Tests

Runtime unit tests cover configuration, resources, logging, and stale temporary-file cleanup;
contract tests preserve existing import paths.
