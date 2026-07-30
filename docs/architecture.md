# Architecture

The pipeline is organized into canonical domain packages with one-way dependencies:

```text
runtime → osm → dataset → publication
   │        │        │             │
   └────────┴────────┴─────────────┴→ workflow → cli
```

`A → B` means B may import A. It does not mean A imports B. Lower-level packages must not import
higher-level packages, and circular imports are forbidden.

## Package boundaries

- `runtime` owns paths, resources, logging, Rich/tqdm terminal presentation, and safe cleanup.
- `osm` owns deterministic PBF discovery and bounded export.
- `dataset` owns schemas, transformations, GeoParquet storage, manifests, and reporting.
- `publication` owns allowlisted upload plans, publication state, retry execution, and Hub
  verification.
- `workflow` composes preflight, builds, resumability, completeness, and publication.
- `cli` exposes the Typer command surface, invokes canonical APIs, and reports results.

Compatibility modules at the package root preserve supported historical imports while delegating
to canonical implementations. They do not introduce a second implementation.

## End-to-end flow

```text
source PBFs
  → deterministic discovery
  → bounded osmium export
  → schema-preserving transformation
  → atomic GeoParquet and manifest generation
  → completeness and publication preflight
  → explicit allowlisted Hugging Face Hub upload
  → remote verification
```

The workflow may stop after local artifact generation. Hub publication is an explicit operation,
not an implicit consequence of importing a package or building GeoParquet.

## Invariants

The reorganization preserves CLI behavior, filesystem boundaries, artifact names and bytes,
resumability, publication allowlists, bounded processing, and deterministic reporting. No domain
move changes the dataset contract.

## Tooling boundary

uv owns dependency resolution and command execution. Ruff is the formatter and
linter, ty is the type checker, and pytest is the test runner. pre-commit and
Just expose the same local gates that GitHub Actions runs in CI. These tools do
not cross the operational boundary into real-data processing or publication.
