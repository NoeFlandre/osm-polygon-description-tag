# Workflow package

## Purpose

Provide the canonical boundary for composing the complete resumable pipeline.

## Responsibilities

Preflight, one-PBF build composition, per-source state transitions, completeness checks, and the
`run-and-publish` lifecycle belong here as they are migrated.

## Non-responsibilities

This package does not redefine lower-level runtime, ingestion, dataset, or publication behavior.

## Public API

Exports are introduced incrementally during the package reorganization. This initial boundary has
no public exports.

## Allowed dependencies

The `runtime`, `osm`, `dataset`, and `publication` packages plus Python's standard library.

## Data flow and side effects

Future workflow APIs will coordinate approved reads, artifact writes, checkpoints, and explicit
publication through the lower-level packages.

## Safety and determinism invariants

Preflight precedes mutation, completed work remains resumable, final completeness is verified, and
publication remains explicit and allowlisted.

## Tests

Workflow unit tests cover preflight, build composition, and orchestration; integration tests cover
resumption and public CLI lifecycles, and contract tests preserve existing imports.
