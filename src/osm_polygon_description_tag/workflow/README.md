# Workflow package

## Purpose

Provide the canonical boundary for composing the complete resumable pipeline.

## Responsibilities

Preflight, one-PBF build composition, per-source state transitions, finalization, and the
`run-and-publish` lifecycle live here. `source_runner.py` owns local build/reuse decisions;
`finalization.py` owns documentation refresh, completeness validation, and final metadata
publication.

## Non-responsibilities

This package does not redefine lower-level runtime, ingestion, dataset, or publication behavior.

## Public API

Import `BuildResult`, `PipelineError`, `build_one`, `build_all`, `safe_osmium_version`,
`PreflightError`, `default_preflight`, `SourceOutcome`, `OrchestrationReport`,
`OrchestratorError`, and `run_and_publish` from `osm_polygon_description_tag.workflow`.
Legacy top-level `pipeline` and `orchestrator` imports remain identity-compatible shims.

## Allowed dependencies

The `runtime`, `osm`, `dataset`, and `publication` packages plus Python's standard library.

## Data flow and side effects

`default_preflight` performs only validation and non-mutating authentication and permission
checks. After approval, builds stream OSM input into atomic local artifacts; orchestration then
creates exact upload plans, verifies remote identities, and atomically records publication state.

## Safety and determinism invariants

Preflight precedes mutation, completed work remains resumable, final completeness is verified, and
publication remains explicit and allowlisted.

## Tests

Workflow unit tests cover preflight, build composition, and orchestration; integration tests cover
resumption and public CLI lifecycles, and contract tests preserve existing imports.
