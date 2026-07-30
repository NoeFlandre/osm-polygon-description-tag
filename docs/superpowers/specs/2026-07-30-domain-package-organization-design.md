# Domain Package Organization Design

## Purpose

Reorganize the Python codebase into cohesive, documented subpackages without
changing the dataset contract, CLI, filesystem behavior, resumability, upload
semantics, or public import paths. Replace mypy completely with Astral ty while
retaining uv as the sole project/dependency runner and Ruff as the sole
formatter and linter.

This is a behavior-preserving architecture refactor. It does not authorize a
real pipeline run, source-data mutation, generated-data mutation, Hugging Face
publication, or dataset cleanup.

## Current constraints

The existing package is functionally complete but structurally flat:

- fifteen Python modules share one package directory;
- `orchestrator.py` is approximately 1,300 lines and owns preflight, state,
  Hub verification, source lifecycle, metadata publication, and top-level
  orchestration;
- `publication.py` combines models, plan construction, safety validation,
  subprocess execution, and retry behavior;
- tests import both public and private names from current module paths and
  monkeypatch selected module boundaries;
- the public CLI entry point is
  `osm_polygon_description_tag.cli:run`;
- operators depend on the exact `run-and-publish` command and its current
  resumability guarantees.

The refactor must preserve those observable contracts.

## Considered approaches

### Domain-oriented subpackages

Group implementation by operational responsibility: runtime support, OSM
ingestion, dataset artifacts, publication, and workflow. Keep thin
compatibility modules at old import paths. This approach matches how files
already change together, gives every directory a clear purpose, and allows the
large orchestration modules to be split along real responsibility boundaries.

This is the selected approach.

### Traditional layered architecture

Separate domain, application, ports, adapters, and infrastructure. This would
create formal dependency inversion but would add abstractions that the
single-purpose dataset pipeline does not currently need.

### Minimal folder grouping

Move existing files into a few directories without splitting them. This has
lower migration effort but would preserve the tangled responsibilities in the
largest modules and provide only cosmetic organization.

## Target source structure

```text
src/osm_polygon_description_tag/
├── __init__.py
├── README.md
├── cli.py
├── config.py
├── discovery.py
├── extraction.py
├── manifest.py
├── orchestrator.py
├── pipeline.py
├── publication.py
├── reporting.py
├── schema.py
├── storage.py
├── transform.py
├── _logging.py
├── _resources.py
├── py.typed
├── _data/
│   ├── dataset-card-template.md
│   └── osmium-export.json
├── runtime/
│   ├── __init__.py
│   ├── README.md
│   ├── cleanup.py
│   ├── config.py
│   ├── logging.py
│   └── resources.py
├── osm/
│   ├── __init__.py
│   ├── README.md
│   ├── discovery.py
│   └── extraction.py
├── dataset/
│   ├── __init__.py
│   ├── README.md
│   ├── manifest.py
│   ├── reporting.py
│   ├── schema.py
│   ├── storage.py
│   └── transform.py
├── publication/
│   ├── __init__.py
│   ├── README.md
│   ├── models.py
│   ├── planning.py
│   ├── state.py
│   ├── upload.py
│   └── verification.py
└── workflow/
    ├── __init__.py
    ├── README.md
    ├── build.py
    ├── orchestrator.py
    └── preflight.py
```

The existing top-level modules remain intentionally. They become documented
compatibility shims that re-export the same supported names from canonical
subpackages. `cli.py` remains the stable console entry point. `config.py`
remains a compatibility module because path defaults are already imported by
external-facing tests and scripts.

## Package responsibilities and dependency direction

### `runtime`

Owns approved paths, packaged-resource lookup, operational logging, and safe
cleanup of abandoned owned temporary files. It has no OSM, Arrow, Hub, or
workflow responsibilities.

Allowed dependencies: Python standard library and static package resources.

### `osm`

Owns deterministic PBF discovery and bounded `osmium export` streaming. It does
not transform rows, write Parquet, publish files, or manage workflow state.

Allowed dependencies: `runtime` and Python standard library.

### `dataset`

Owns the versioned Arrow/GeoParquet schema, record transformation, atomic
storage and validation, manifests, deterministic statistics, and dataset-card
generation.

Allowed dependencies: `runtime`, `osm` data types, PyArrow, Shapely, PyProj,
DuckDB, and Python standard library.

### `publication`

Owns upload models, explicit allowlisted plans, local publication state,
bounded retry execution, remote Hub verification, and managed remote
reconciliation. It does not discover PBFs or build dataset rows.

Allowed dependencies: `runtime`, stable dataset artifact validation and
manifest APIs, `huggingface_hub`, and Python standard library.

### `workflow`

Owns preflight, one-PBF build composition, the per-source resumable state
machine, final completeness, and the top-level `run-and-publish` lifecycle.

Allowed dependencies: `runtime`, `osm`, `dataset`, and `publication`.

### `cli`

Parses public arguments, invokes documented workflow/dataset/publication APIs,
maps expected failures to exit codes, and prints final JSON. No lower package
may import from `cli`.

The dependency direction is shown below. An arrow `A → B` means that `B` may
import from `A`; it does not mean that `A` imports from `B`.

```text
runtime ───▶ osm ───▶ dataset ───▶ publication
   │          │          │               │
   └──────────┴──────────┴───────────────┴──▶ workflow ───▶ cli
```

Circular imports and imports from higher-level packages into lower-level
packages are forbidden.

## Compatibility contract

The following existing imports remain supported:

```python
from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.discovery import discover_sources
from osm_polygon_description_tag.extraction import ExportRecord
from osm_polygon_description_tag.manifest import read_manifest
from osm_polygon_description_tag.orchestrator import run_and_publish
from osm_polygon_description_tag.pipeline import build_one
from osm_polygon_description_tag.publication import create_upload_plan
from osm_polygon_description_tag.reporting import generate_dataset_docs
from osm_polygon_description_tag.schema import SCHEMA
from osm_polygon_description_tag.storage import validate_geoparquet
from osm_polygon_description_tag.transform import transform_record
```

Each compatibility module contains:

- a module docstring naming the canonical package;
- explicit imports rather than wildcard exports;
- an explicit `__all__`;
- no independent business logic or mutable state.

Private monkeypatch targets used by the repository tests are migrated to
canonical modules. Compatibility tests cover public names, not indefinite
support for every historical private implementation detail. Where a private
target is part of a production test seam, the canonical module exposes one
documented seam rather than duplicating mutable globals across shims.

The console entry point, CLI flags, JSON report, exit codes, log locations,
source/data defaults, artifact names, manifests, Parquet bytes, dataset-card
generation, and Hub paths remain unchanged.

## Documentation contract

Every source package directory contains a `README.md`. Each package README has
the same sections:

1. purpose;
2. responsibilities;
3. non-responsibilities;
4. public API with canonical import examples;
5. allowed dependencies;
6. data flow and side effects;
7. safety and determinism invariants;
8. associated unit, contract, and integration tests.

Additional documentation:

- `src/osm_polygon_description_tag/README.md` maps the complete package and
  explains canonical versus compatibility imports.
- `docs/architecture.md` documents dependency direction and end-to-end flow.
- `docs/development.md` documents uv setup, Ruff, ty, pytest, TDD, focused
  checks, and release gates.
- the root `README.md` stays operator-focused and links to architecture and
  development documentation.
- compatibility modules use concise module docstrings instead of separate
  duplicated documentation.

Developer documentation must not copy generated dataset counts or statistics.
Dataset facts remain derived from Parquet and manifests.

## Test organization

Tests mirror the source domains:

```text
tests/
├── conftest.py
├── fixtures/
├── unit/
│   ├── runtime/
│   ├── osm/
│   ├── dataset/
│   ├── publication/
│   └── workflow/
├── contracts/
└── integration/
```

Unit tests move with the canonical implementation they exercise. Contract
tests retain CLI, schema, dataset-card, package-layout, import-compatibility,
and dependency-direction assertions. Integration tests retain real-osmium and
multi-run lifecycle coverage.

Tests do not gain live network access. Existing hermetic Hugging Face guards
remain effective after module movement.

## Incremental migration

Migration proceeds in independently green commits:

1. add package-layout, documentation, dependency-direction, and compatibility
   contract tests;
2. create documented `runtime` and move config/resources/logging/cleanup;
3. create documented `osm` and move discovery/extraction;
4. create documented `dataset` and move schema/transform/storage/manifest/
   reporting;
5. create documented `publication` and split models/planning/upload/
   verification/state;
6. create documented `workflow` and split build/preflight/orchestration;
7. convert top-level modules into explicit compatibility shims;
8. mirror unit-test folders and update canonical test imports;
9. replace mypy with ty and finalize developer/architecture documentation.

Each step updates imports only within its scope, runs focused tests, and leaves
the CLI usable. No mass search-and-replace is accepted without focused
verification.

## uv, Ruff, and ty toolchain

`uv` remains the only supported dependency and command runner. `uv.lock`
records exact tool versions. ty is added to the development dependency group
using the official `uv add --dev ty` workflow. mypy is removed from:

- development dependencies;
- `uv.lock`;
- `[tool.mypy]` and mypy overrides;
- active operator/developer documentation and verification commands;
- automation or scripts, if present.

Historical design records remain unchanged when they describe the toolchain
that was in use when those records were written.

The project uses `[tool.ty]` configuration in `pyproject.toml`. The source root
is `src`, the target Python version is 3.12, and warnings fail the check.
Third-party import handling is configured only when ty demonstrates a concrete
diagnostic; broad `Any` replacement or blanket rule disabling is prohibited.

Canonical commands are:

```bash
uv sync
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov=osm_polygon_description_tag
uv build
```

## TDD and verification

Structural contract tests are written and observed failing before source
movement. Every migration task follows RED, GREEN, and refactor:

1. add or update the narrow contract test;
2. run it and confirm the expected structural/import failure;
3. make the smallest source move or split;
4. run focused unit and contract tests;
5. run ty and Ruff on the changed domain;
6. commit the green step.

After publication and workflow movement, run the synthetic real-osmium and
three-run public CLI lifecycle tests. The final release gates are:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing
uv build
git diff --check
git status --short --branch
```

Coverage remains at least 90 percent with zero failed or skipped tests.
Verification also compares the raw-source and generated-data roots before and
after the refactor. No real PBF is opened and no Hugging Face or GitHub
mutation occurs during implementation.

## Acceptance criteria

The refactor is complete only when:

- all five canonical subpackages exist and contain their required README;
- no canonical implementation remains duplicated in top-level shims;
- dependency-direction tests pass without exceptions;
- documented canonical imports work;
- supported legacy imports work;
- the exact public CLI and resumable lifecycle remain unchanged;
- mypy is absent from dependencies, lockfile, configuration, active
  operator/developer documentation, and commands;
- `uv run ty check`, both Ruff gates, and the full pytest/coverage gate pass;
- package data is included correctly in a built wheel;
- external raw and generated-data roots are unchanged;
- Git status is clean after focused commits.
