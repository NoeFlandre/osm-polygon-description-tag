# Publication package

## Purpose
Plan, execute, verify, and record guarded Hugging Face publication.

## Responsibilities
Own exact upload plans, bounded retries, remote identity verification,
reconciliation of managed remote namespaces, and atomic publication state.

## Non-responsibilities
This package does not extract OSM features, transform geometry, write
GeoParquet, calculate dataset statistics, or render terminal presentation.

## Public API
The package root exports the supported planning, upload, verification, and
state interfaces. Top-level compatibility modules only re-export them.

## Allowed dependencies
Publication may depend on runtime configuration, filesystem helpers, and
validated dataset manifests. Dataset and workflow code may call publication;
publication must not import workflow.

## Data flow and side effects
Validated local artifacts become exact allowlisted upload plans. Uploads are
verified against the Hub before an atomic state update records completion.
Network and subprocess side effects occur only through explicit public calls.

## Safety and determinism invariants
Plans reject symlinks, temporary files, unknown paths, stale manifests, and
identity drift. Only managed `data/` and `manifests/` remote paths are
reconciled; retry and state transitions are deterministic and resumable.

## Tests
Hermetic unit tests live under `tests/unit/publication`; lifecycle coverage
uses faked Hub boundaries and never publishes.
