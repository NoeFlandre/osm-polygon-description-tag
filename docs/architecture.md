# Architecture

The pipeline has canonical domain packages. The dependencies go in one direction:

```text
runtime → osm → dataset → publication
   │        │        │             │
   └────────┴────────┴─────────────┴→ workflow → cli
```

`A → B` means that B can import A. It does not mean that A imports B. A
lower-level package must not import a higher-level package. Circular imports
are not permitted.

## Container boundary

The `Dockerfile` separates the application code from the operator data. The
base stage installs the real `osmium-tool`. The build stage installs the locked
Python environment. The development stage adds the checkout and the test
dependencies. The runtime stage copies only the non-editable installed package
into a non-root image. The directory `/data` is the only mounted state
boundary. The default command is `--help`. To start processing, you must use
`run-and-publish --source-root /source --data-root /data`. The raw source mount
is read-only. The resumable state stays on the host. Thus it remains when you
remove the container.

## Package boundaries

- `runtime` owns paths, resources, logging, Rich/tqdm terminal presentation, and safe cleanup.
- `osm` owns deterministic PBF discovery and bounded export.
- `dataset` owns schemas, transformations, GeoParquet storage, manifests, and reporting.
- `publication` owns allowlisted upload plans, publication state, retry execution, and Hub
  verification.
- `observability` owns optional Trackio metrics for dataset snapshots and live resumable
  runs. A Trackio failure does not affect the dataset artifacts.
- `workflow` composes preflight, builds, resumability, completeness, and publication.
- `cli` exposes the Typer command surface, calls the canonical APIs, and reports the results.

The package root holds only the console modules (`cli`, `language_cli`). Each
other item is imported from its domain package.

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

The workflow can stop after the local artifact generation. The Hub publication
is an explicit operation. Importing a package or building GeoParquet does not
start it.

## Invariants

The reorganization keeps these items unchanged: CLI behavior, filesystem
boundaries, artifact names and bytes, resumability, publication allowlists,
bounded processing, and deterministic reporting. No domain move changes the
dataset contract.

## Tooling boundary

uv owns dependency resolution and command execution. Ruff is the formatter and
the linter. ty is the type checker. pytest is the test runner. pre-commit and
Just give the same local gates that GitHub Actions runs in CI. These tools do
not cross the operational boundary into real-data processing or publication.

## Documentation boundary

The MkDocs pages describe the stable public contracts and the operator
workflows. The package READMEs describe the responsibilities of the canonical
modules. The generated dataset-card template describes the published artifact.
It is separate from the site on purpose. Internal planning material is not part
of the public documentation site.
