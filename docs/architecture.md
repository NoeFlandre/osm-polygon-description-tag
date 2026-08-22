# Architecture

The pipeline is organized into canonical domain packages with one-way dependencies:

```text
runtime → osm → dataset → publication
   │        │        │             │
   └────────┴────────┴─────────────┴→ workflow → cli
```

`A → B` means B may import A. It does not mean A imports B. Lower-level packages must not import
higher-level packages, and circular imports are forbidden.

## Container boundary

The `Dockerfile` separates application code from operator data. Its base stage
installs the real `osmium-tool`; the build stage installs the locked Python
environment; development adds the checkout and test dependencies; runtime
copies only the non-editable installed package into a non-root image. `/data`
is the sole mounted state boundary. The default command is `--help`, while
`run-and-publish --source-root /data/raw --data-root /data` is required to start
processing. The raw source mount is read-only, and resumable state survives
container removal because it remains on the host.

## Package boundaries

- `runtime` owns paths, resources, logging, Rich/tqdm terminal presentation, and safe cleanup.
- `osm` owns deterministic PBF discovery and bounded export.
- `dataset` owns schemas, transformations, GeoParquet storage, manifests, and reporting.
- `publication` owns allowlisted upload plans, publication state, retry execution, and Hub
  verification.
- `observability` owns optional Trackio metrics for dataset snapshots and live resumable
  runs; Trackio failures never affect dataset artifacts.
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
  → global OSM identity deduplication
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

## Documentation boundary

MkDocs pages describe stable public contracts and operator workflows. Package
READMEs document canonical module responsibilities. The generated dataset-card
template describes the published artifact and is intentionally kept separate
from the site. Internal planning material is not part of the public
documentation site.
