# Domain Package Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the flat package into documented domain subpackages, preserve the public CLI and legacy imports, and replace mypy completely with ty under uv.

**Architecture:** Canonical implementations move into `runtime`, `osm`, `dataset`, `publication`, and `workflow`; existing top-level modules become explicit compatibility shims. Dependencies flow from runtime through the domain packages into workflow and then CLI, while contract tests prevent reverse imports, implementation duplication, undocumented packages, and public API drift.

**Tech Stack:** Python 3.12, uv, Ruff, Astral ty, pytest, pytest-cov, importlib, PyArrow, Shapely, PyProj, DuckDB, huggingface_hub, osmium-tool.

---

## File map

New canonical implementation files:

- `src/osm_polygon_description_tag/runtime/{__init__.py,README.md,config.py,resources.py,logging.py,cleanup.py}`
- `src/osm_polygon_description_tag/osm/{__init__.py,README.md,discovery.py,extraction.py}`
- `src/osm_polygon_description_tag/dataset/{__init__.py,README.md,schema.py,transform.py,storage.py,manifest.py,reporting.py}`
- `src/osm_polygon_description_tag/publication/{__init__.py,README.md,models.py,planning.py,upload.py,verification.py,state.py}`
- `src/osm_polygon_description_tag/workflow/{__init__.py,README.md,build.py,preflight.py,orchestrator.py}`

Stable entry points and compatibility files:

- `src/osm_polygon_description_tag/cli.py` remains the console entry point and imports canonical APIs.
- Existing `_logging.py`, `_resources.py`, `config.py`, `discovery.py`, `extraction.py`, `manifest.py`, `orchestrator.py`, `pipeline.py`, `reporting.py`, `schema.py`, `storage.py`, and `transform.py` become explicit re-export shims.
- Existing `publication.py` is atomically replaced by the `publication/` package; `publication/__init__.py` preserves the exact public import path and exports.

Documentation and project configuration:

- Create `src/osm_polygon_description_tag/README.md`.
- Create `docs/architecture.md` and `docs/development.md`.
- Update root `README.md`, `pyproject.toml`, and `uv.lock`.

Tests:

- Create `tests/contracts/test_package_layout.py`, `test_import_compatibility.py`, and `test_dependency_direction.py`.
- Move flat unit tests into `tests/unit/{runtime,osm,dataset,publication,workflow}`.
- Keep CLI/schema/card contracts in `tests/contracts` and end-to-end scenarios in `tests/integration`.

### Task 1: Pin the package-layout and documentation contract

**Files:**
- Create: `tests/contracts/test_package_layout.py`
- Modify: `tests/test_project_foundation.py`

- [ ] **Step 1: Write the failing package-layout test**

```python
from pathlib import Path

import osm_polygon_description_tag


STAGED_PACKAGES = ("runtime", "osm", "dataset", "workflow")
CANONICAL_PACKAGES = (*STAGED_PACKAGES, "publication")
README_SECTIONS = (
    "## Purpose",
    "## Responsibilities",
    "## Non-responsibilities",
    "## Public API",
    "## Allowed dependencies",
    "## Data flow and side effects",
    "## Safety and determinism invariants",
    "## Tests",
)


def test_canonical_packages_are_documented() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    for package_name in STAGED_PACKAGES:
        package = package_root / package_name
        assert (package / "__init__.py").is_file()
        text = (package / "README.md").read_text(encoding="utf-8")
        for section in README_SECTIONS:
            assert section in text


def test_package_and_project_architecture_docs_exist() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    project_root = package_root.parents[1]
    assert (package_root / "README.md").is_file()
    assert (project_root / "docs" / "architecture.md").is_file()
    assert (project_root / "docs" / "development.md").is_file()
```

- [ ] **Step 2: Run the contract to verify RED**

Run: `uv run pytest tests/contracts/test_package_layout.py -q`

Expected: FAIL because the five canonical package directories and documentation files do not exist.

- [ ] **Step 3: Add empty package boundaries and complete README skeletons**

Create each `__init__.py` for `runtime`, `osm`, `dataset`, and `workflow` with a
domain docstring and an empty explicit export list. Do not create
`publication/` yet because it would shadow and break the existing
`publication.py` module.

```python
"""Canonical runtime support APIs."""

__all__: list[str] = []
```

Use the matching domain name in each file. Create these four package READMEs
with all eight required headings and factual domain-specific content from the
approved design. Do not claim APIs have moved yet; state that exports are
introduced incrementally. The publication README is created atomically in
Task 5.

- [ ] **Step 4: Add package-map and development documents**

Create `src/osm_polygon_description_tag/README.md` with the canonical package map and compatibility policy. Create `docs/architecture.md` with the dependency rule “A → B means B may import A” and the end-to-end source→GeoParquet→Hub flow. Create `docs/development.md` with the current commands initially, explicitly marking the ty migration as a later task in this plan.

- [ ] **Step 5: Run the focused contract and foundation tests**

Run: `uv run pytest tests/contracts/test_package_layout.py tests/test_project_foundation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/osm_polygon_description_tag/README.md \
  src/osm_polygon_description_tag/runtime \
  src/osm_polygon_description_tag/osm \
  src/osm_polygon_description_tag/dataset \
  src/osm_polygon_description_tag/workflow \
  docs/architecture.md docs/development.md \
  tests/contracts/test_package_layout.py tests/test_project_foundation.py
git commit -m "test: pin documented domain package layout"
```

### Task 2: Move runtime support behind compatibility shims

**Files:**
- Move: `src/osm_polygon_description_tag/config.py` → `runtime/config.py`
- Move: `src/osm_polygon_description_tag/_resources.py` → `runtime/resources.py`
- Move: `src/osm_polygon_description_tag/_logging.py` → `runtime/logging.py`
- Create: `src/osm_polygon_description_tag/runtime/cleanup.py`
- Recreate: top-level `config.py`, `_resources.py`, `_logging.py`
- Move tests: `test_config.py`, `test_resources.py`, `test_logging_architecture.py`, `test_stale_temp_cleanup.py` → `tests/unit/runtime/`
- Create: `tests/contracts/test_import_compatibility.py`

- [ ] **Step 1: Write failing legacy/canonical import assertions**

```python
from osm_polygon_description_tag import _logging, _resources, config
from osm_polygon_description_tag.runtime import config as runtime_config
from osm_polygon_description_tag.runtime import logging as runtime_logging
from osm_polygon_description_tag.runtime import resources as runtime_resources


def test_runtime_legacy_imports_are_identical() -> None:
    assert config.Paths is runtime_config.Paths
    assert config.UnsafePathError is runtime_config.UnsafePathError
    assert _logging.RunLogger is runtime_logging.RunLogger
    assert _resources.osmium_export_config is runtime_resources.osmium_export_config
    assert _resources.dataset_card_template is runtime_resources.dataset_card_template
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_import_compatibility.py -q`

Expected: FAIL because canonical runtime modules do not exist.

- [ ] **Step 3: Move the implementations and extract cleanup**

Use `git mv` for the three modules. Move `cleanup_stale_owned_temps`, `_OWNED_TEMP_PATTERN`, and only their required imports from the old orchestrator into `runtime/cleanup.py`. Its public surface is:

```python
__all__ = ["cleanup_stale_owned_temps"]
```

Update internal imports in the moved runtime files only. Do not change behavior.

- [ ] **Step 4: Create explicit compatibility shims**

Use this exact pattern, expanding all currently supported names:

```python
"""Compatibility imports; canonical APIs live in `.runtime.config`."""

from osm_polygon_description_tag.runtime.config import (
    DEFAULT_DATA_ROOT,
    DEFAULT_SOURCE_ROOT,
    Paths,
    UnsafePathError,
)

__all__ = ["DEFAULT_DATA_ROOT", "DEFAULT_SOURCE_ROOT", "Paths", "UnsafePathError"]
```

Apply the same explicit-import/`__all__` pattern to `_resources.py` and `_logging.py`. No shim may define functions, classes, mutable state, or wrappers.

- [ ] **Step 5: Export documented runtime APIs**

In `runtime/__init__.py`, explicitly export `Paths`, `UnsafePathError`, `RunLogger`, `configure_rotation`, resource locators, and `cleanup_stale_owned_temps`. Update `runtime/README.md` examples to import these names from `osm_polygon_description_tag.runtime`.

- [ ] **Step 6: Move and update focused tests**

Create `tests/unit/runtime/__init__.py`, use `git mv` for the four runtime test files, and update monkeypatch targets to canonical module paths. Keep the compatibility contract importing legacy paths.

- [ ] **Step 7: Verify GREEN**

Run: `uv run pytest tests/unit/runtime tests/contracts/test_import_compatibility.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/osm_polygon_description_tag tests/unit/runtime \
  tests/contracts/test_import_compatibility.py
git commit -m "refactor: organize runtime support package"
```

### Task 3: Move OSM discovery and extraction

**Files:**
- Move: `discovery.py` → `osm/discovery.py`
- Move: `extraction.py` → `osm/extraction.py`
- Recreate: top-level `discovery.py`, `extraction.py`
- Move tests: `test_discovery.py`, `test_extraction.py`, `test_extraction_stream.py`, `test_real_osmium_coverage.py` → `tests/unit/osm/`
- Modify: `tests/contracts/test_import_compatibility.py`

- [ ] **Step 1: Extend the failing compatibility contract**

```python
from osm_polygon_description_tag import discovery, extraction
from osm_polygon_description_tag.osm import discovery as osm_discovery
from osm_polygon_description_tag.osm import extraction as osm_extraction


def test_osm_legacy_imports_are_identical() -> None:
    assert discovery.Source is osm_discovery.Source
    assert discovery.discover_sources is osm_discovery.discover_sources
    assert extraction.ExportRecord is osm_extraction.ExportRecord
    assert extraction.stream_export is osm_extraction.stream_export
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_import_compatibility.py::test_osm_legacy_imports_are_identical -q`

Expected: FAIL because canonical OSM modules do not exist.

- [ ] **Step 3: Move modules and create shims**

Use `git mv`, then recreate top-level shims with explicit imports and `__all__`. `osm/discovery.py` exports `Source` and `discover_sources`; `osm/extraction.py` exports `STDERR_CAP_BYTES`, `OsmiumExportError`, `ExportRecord`, `export_command`, `parse_copy_record`, `iter_records`, `stream_export`, and `osmium_version`.

- [ ] **Step 4: Export and document canonical APIs**

Populate `osm/__init__.py` with the public names above. Update `osm/README.md` with exact command construction, immutable-source behavior, bounded stderr/streaming, and the fact that this package never writes Parquet.

- [ ] **Step 5: Move tests and update canonical imports**

Use `git mv` into `tests/unit/osm/`. Change direct implementation imports to `osm.discovery` and `osm.extraction`; leave compatibility assertions only in the contract test.

- [ ] **Step 6: Verify GREEN**

Run: `uv run pytest tests/unit/osm tests/contracts/test_import_compatibility.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag tests/unit/osm \
  tests/contracts/test_import_compatibility.py
git commit -m "refactor: organize OSM ingestion package"
```

### Task 4: Move the dataset contract, transformation, storage, manifests, and reporting

**Files:**
- Move: `schema.py`, `transform.py`, `storage.py`, `manifest.py`, `reporting.py` → `dataset/`
- Recreate: five top-level compatibility shims
- Move dataset unit tests into `tests/unit/dataset/`
- Modify: schema/card contracts and import-compatibility contract

- [ ] **Step 1: Add canonical/legacy identity checks**

```python
from osm_polygon_description_tag import manifest, reporting, schema, storage, transform
from osm_polygon_description_tag.dataset import manifest as dataset_manifest
from osm_polygon_description_tag.dataset import reporting as dataset_reporting
from osm_polygon_description_tag.dataset import schema as dataset_schema
from osm_polygon_description_tag.dataset import storage as dataset_storage
from osm_polygon_description_tag.dataset import transform as dataset_transform


def test_dataset_legacy_imports_are_identical() -> None:
    assert schema.SCHEMA is dataset_schema.SCHEMA
    assert transform.transform_record is dataset_transform.transform_record
    assert storage.validate_geoparquet is dataset_storage.validate_geoparquet
    assert manifest.Manifest is dataset_manifest.Manifest
    assert reporting.generate_dataset_docs is dataset_reporting.generate_dataset_docs
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_import_compatibility.py::test_dataset_legacy_imports_are_identical -q`

Expected: FAIL because canonical dataset modules do not exist.

- [ ] **Step 3: Move implementations and fix only canonical imports**

Use `git mv`. Apply these dependency changes:

```python
# dataset/transform.py
from osm_polygon_description_tag.osm.extraction import ExportRecord

# dataset/storage.py and dataset/reporting.py
from osm_polygon_description_tag.dataset.schema import SCHEMA, SCHEMA_VERSION, geo_metadata

# dataset/manifest.py
from osm_polygon_description_tag.runtime.resources import osmium_export_config, project_code_revision
from osm_polygon_description_tag.dataset.schema import GEOPARQUET_VERSION, SCHEMA_VERSION
```

Use only the names each file already needs. Do not alter schema fields, manifest versions, artifact bytes, or statistics.

- [ ] **Step 4: Create explicit top-level shims and canonical exports**

Every shim explicitly imports all existing public constants, dataclasses, exceptions, and functions and declares `__all__`. Populate `dataset/__init__.py` with the stable high-level APIs: `SCHEMA`, version constants, `transform_record`, `write_geoparquet`, `validate_geoparquet`, `Manifest`, manifest read/write/identity functions, `collect_stats`, and `generate_dataset_docs`.

- [ ] **Step 5: Move dataset tests**

Move these files into `tests/unit/dataset/`: `test_bounded_memory_verifier.py`, `test_deterministic_docs.py`, `test_disk_backed_uniqueness.py`, `test_final_metadata.py`, `test_geodesic_area.py`, `test_identity_drift.py`, `test_manifest.py`, `test_name_schema.py`, `test_reporting.py`, `test_reporting_bounded.py`, `test_reporting_helpers.py`, `test_resumability_contract.py`, `test_stable_resumability.py`, `test_storage.py`, `test_storage_bounded.py`, and `test_transform.py`. Update implementation imports and monkeypatch targets to canonical modules.

- [ ] **Step 6: Run dataset and contract tests**

Run:

```bash
uv run pytest tests/unit/dataset \
  tests/contracts/test_schema_contract.py \
  tests/contracts/test_dataset_card.py \
  tests/contracts/test_import_compatibility.py -q
```

Expected: PASS with unchanged schema and generated-card assertions.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag tests/unit/dataset tests/contracts
git commit -m "refactor: organize dataset artifact package"
```

### Task 5: Split publication into models, planning, upload, verification, and state

**Files:**
- Create: `publication/models.py`, `planning.py`, `upload.py`, `verification.py`, `state.py`
- Replace atomically: top-level `publication.py` with `publication/__init__.py`
- Move publication tests into `tests/unit/publication/`
- Modify: `tests/contracts/test_import_compatibility.py`

- [ ] **Step 1: Add publication identity checks**

```python
from osm_polygon_description_tag import publication as legacy
from osm_polygon_description_tag.publication import (
    PublicationError,
    UploadItem,
    UploadPlan,
    create_upload_plan,
    execute_upload,
)


def test_publication_legacy_imports_are_identical() -> None:
    assert legacy.UploadPlan is UploadPlan
    assert legacy.UploadItem is UploadItem
    assert legacy.PublicationError is PublicationError
    assert legacy.create_upload_plan is create_upload_plan
    assert legacy.execute_upload is execute_upload
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_import_compatibility.py::test_publication_legacy_imports_are_identical -q`

Expected: FAIL because the canonical publication submodules do not exist.

- [ ] **Step 3: Extract publication models**

Move `REPO_ID`, retry constants, `Runner`, `PublicationError`, `PublishRetry`, `UploadItem`, and `UploadPlan` into `publication/models.py`. Preserve dataclass definitions and payload/identity behavior byte-for-byte. Export them explicitly from `publication/__init__.py`.

- [ ] **Step 4: Extract planning**

Move `_build_item`, `_validate_manifest`, `_collect_allowlisted_files`, `create_upload_plan`, `_build_per_pbf_upload_plan`, `_build_metadata_only_upload_plan`, `per_pbf_command`, and `metadata_only_command` into `publication/planning.py`. Import manifest/storage APIs from `dataset`. Preserve the exact allowlists for `.cache/huggingface`, `.work`, logs, data, manifests, metadata, and owned temporaries.

- [ ] **Step 5: Extract upload execution**

Move `_verify_identity`, `_build_command`, `_classify_failure`, `_default_runner_with_retry`, and `execute_upload` into `publication/upload.py`. Import models and plan types from sibling modules. Preserve retry count, timeout propagation, callback events, exact `hf upload-large-folder --include` arguments, and immediate `KeyboardInterrupt`.

- [ ] **Step 6: Extract Hub verification and publication state**

Move `_HuggingFaceHub`, `HubVerifier`, factories, SHA verification, and `HubVerificationError` from the old orchestrator into `publication/verification.py`. Move `PUBLICATION_STATE_FILENAME`, `_atomic_write_json`, `read_publication_state`, state match/write helpers, and final metadata state helpers into `publication/state.py`. Keep state writes atomic and confined to the data root.

- [ ] **Step 7: Finish exports and compatibility shim**

Prepare all focused files in a temporary same-checkout staging directory,
then remove `publication.py`, create `publication/`, and add the prepared
files in one working-tree step. Export all supported public names explicitly
from `publication/__init__.py`, which is both the canonical API and the
compatibility surface for the unchanged import path. Run the focused tests
immediately; do not commit a state in which both the old module and empty
package exist.

- [ ] **Step 8: Move publication tests and verify**

Move all `test_publication*.py`, `test_per_pbf_plan.py`, `test_uploader_cache_allowance.py`, `test_hub_verification.py`, `test_default_hub_verifier.py`, `test_real_hf_verification.py`, `test_metadata_resumability.py`, `test_auth_check_preflight.py`, `test_timeout_propagation.py`, and `test_retry_timeout.py` into `tests/unit/publication/`. Update implementation imports and monkeypatch targets.

Run: `uv run pytest tests/unit/publication tests/contracts/test_import_compatibility.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -A src/osm_polygon_description_tag/publication.py \
  src/osm_polygon_description_tag/publication \
  tests/unit/publication tests/contracts/test_import_compatibility.py
git commit -m "refactor: split publication package by responsibility"
```

### Task 6: Split build, preflight, and orchestration workflow

**Files:**
- Move/split: `pipeline.py` → `workflow/build.py`
- Split: `orchestrator.py` → `workflow/preflight.py`, `workflow/orchestrator.py`
- Recreate: top-level `pipeline.py`, `orchestrator.py`
- Move workflow tests into `tests/unit/workflow/`
- Modify: import-compatibility and CLI contract tests

- [ ] **Step 1: Pin canonical and legacy workflow APIs**

```python
from osm_polygon_description_tag import orchestrator as legacy_orchestrator
from osm_polygon_description_tag import pipeline as legacy_pipeline
from osm_polygon_description_tag.workflow import (
    BuildResult,
    OrchestrationReport,
    build_all,
    build_one,
    run_and_publish,
)


def test_workflow_legacy_imports_are_identical() -> None:
    assert legacy_pipeline.BuildResult is BuildResult
    assert legacy_pipeline.build_one is build_one
    assert legacy_pipeline.build_all is build_all
    assert legacy_orchestrator.OrchestrationReport is OrchestrationReport
    assert legacy_orchestrator.run_and_publish is run_and_publish
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_import_compatibility.py::test_workflow_legacy_imports_are_identical -q`

Expected: FAIL because canonical workflow exports do not exist.

- [ ] **Step 3: Move the build pipeline**

Use `git mv pipeline.py workflow/build.py`. Update imports to canonical `runtime`, `osm`, and `dataset` modules. Recreate top-level `pipeline.py` as an explicit shim exporting `PipelineError`, `BuildResult`, `safe_osmium_version`, `build_one`, and `build_all`.

- [ ] **Step 4: Extract preflight**

Move `PreflightError`, `Preflight`, `_probe_osmium_version`, and `default_preflight` into `workflow/preflight.py`. Import `Paths` from runtime, Hub verification interfaces from publication, and resource paths from runtime. Preserve the no-mutation-before-auth contract.

- [ ] **Step 5: Reduce the canonical orchestrator**

Move `SourceOutcome`, `OrchestrationReport`, status constants, `_local_artifact_is_complete`, `_process_one`, `_execute_publication`, `run_and_publish`, `_run_and_publish`, `_verify_final_completeness`, `_upload_final_metadata`, and `_default_clock` into `workflow/orchestrator.py`. Replace moved helpers with imports from `runtime.cleanup`, `publication.state`, `publication.verification`, `publication.planning`, and `workflow.preflight`.

- [ ] **Step 6: Create workflow exports and legacy shim**

Populate `workflow/__init__.py` with documented exceptions, result types, `build_one`, `build_all`, `default_preflight`, and `run_and_publish`. Replace top-level `orchestrator.py` with explicit re-exports. For repository-owned private test seams, update tests to canonical modules; do not duplicate globals in the shim.

- [ ] **Step 7: Move workflow tests**

Move `test_pipeline.py`, `test_preflight_error_paths.py`,
`test_preflight_hardening.py`, `test_local_data_boundary.py`, and
`test_cli_no_test_hooks.py` into `tests/unit/workflow/`. If orchestration tests
are currently distributed across other named files, classify them by the APIs
they exercise rather than inventing a missing `test_orchestrator.py`. Update
canonical imports and monkeypatch targets.

- [ ] **Step 8: Verify workflow and integration behavior**

Run:

```bash
uv run pytest tests/unit/workflow \
  tests/contracts/test_cli_contract.py \
  tests/contracts/test_import_compatibility.py \
  tests/integration/test_end_to_end.py \
  tests/integration/test_public_cli_lifecycle.py \
  tests/integration/test_run_and_publish_dry_run.py \
  tests/integration/test_three_run_public.py \
  tests/integration/test_three_run_scenario.py -q
```

Expected: PASS, including interruption exit 130, reuse after interruption, no-op third run, exact per-PBF upload plans, and deterministic metadata.

- [ ] **Step 9: Commit**

```bash
git add src/osm_polygon_description_tag tests/unit/workflow \
  tests/contracts tests/integration
git commit -m "refactor: organize resumable workflow package"
```

### Task 7: Point the CLI at canonical APIs and prove no compatibility implementation remains

**Files:**
- Modify: `src/osm_polygon_description_tag/cli.py`
- Create: `tests/contracts/test_dependency_direction.py`
- Modify: `tests/contracts/test_cli_contract.py`, `test_import_compatibility.py`

- [ ] **Step 1: Write the failing dependency-direction test**

```python
import ast
from pathlib import Path

import osm_polygon_description_tag


ALLOWED = {
    "runtime": {"runtime"},
    "osm": {"runtime", "osm"},
    "dataset": {"runtime", "osm", "dataset"},
    "publication": {"runtime", "dataset", "publication"},
    "workflow": {"runtime", "osm", "dataset", "publication", "workflow"},
}


def test_canonical_packages_follow_dependency_direction() -> None:
    root = Path(osm_polygon_description_tag.__file__).parent
    for owner, allowed in ALLOWED.items():
        for path in (root / owner).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module is None:
                    continue
                prefix = "osm_polygon_description_tag."
                if node.module.startswith(prefix):
                    imported_domain = node.module.removeprefix(prefix).split(".", 1)[0]
                    assert imported_domain in allowed, (
                        f"{path.relative_to(root)} imports forbidden domain "
                        f"{imported_domain}"
                    )
```

Also add a shim-purity test that parses each compatibility module and permits only a docstring, imports, `__all__`, and type-checking declarations.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_dependency_direction.py -q`

Expected: FAIL until remaining imports and shim bodies are canonicalized.

- [ ] **Step 3: Update CLI imports**

Import resources/config from `runtime`, discovery from `osm`, artifact APIs from `dataset`, publication APIs from `publication`, and build/orchestration APIs from `workflow`. Do not change parser definitions, defaults, output JSON, exception mapping, or exit codes.

- [ ] **Step 4: Remove residual top-level implementation**

Run:

```bash
rg -n "^def |^class |^[A-Z][A-Z0-9_]+ =" \
  src/osm_polygon_description_tag/{_logging,_resources,config,discovery,extraction,manifest,orchestrator,pipeline,reporting,schema,storage,transform}.py
```

Expected: no business functions/classes/constants are defined in shims; only imported names and `__all__` remain. Adjust shims until the AST contract passes.

- [ ] **Step 5: Verify CLI and dependency contracts**

Run:

```bash
uv run pytest tests/contracts/test_dependency_direction.py \
  tests/contracts/test_import_compatibility.py \
  tests/contracts/test_cli_contract.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/osm_polygon_description_tag/cli.py \
  src/osm_polygon_description_tag/*.py \
  tests/contracts
git commit -m "refactor: enforce canonical dependency direction"
```

### Task 8: Finish the mirrored unit-test organization

**Files:**
- Move: all remaining flat `tests/test_*.py` into the correct `tests/unit/<domain>/`
- Modify: `tests/conftest.py`, moved tests
- Create: `tests/unit/__init__.py` and domain `__init__.py` files

- [ ] **Step 1: Add a failing test-layout contract**

Append to `test_package_layout.py`:

```python
def test_unit_tests_mirror_source_domains() -> None:
    project_root = Path(osm_polygon_description_tag.__file__).parent.parents[1]
    tests_root = project_root / "tests"
    assert not list(tests_root.glob("test_*.py"))
    for domain in CANONICAL_PACKAGES:
        assert (tests_root / "unit" / domain).is_dir()
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_package_layout.py::test_unit_tests_mirror_source_domains -q`

Expected: FAIL while flat unit tests remain.

- [ ] **Step 3: Classify and move remaining tests**

Move configuration/logging/resource safety tests to runtime; discovery/extraction/osmium tests to osm; schema/transform/storage/manifest/reporting tests to dataset; plans/uploads/Hub/state/retry tests to publication; pipeline/preflight/orchestrator tests to workflow. Keep only shared fixtures in `tests/conftest.py`, public contracts in `tests/contracts`, and multi-component scenarios in `tests/integration`.

- [ ] **Step 4: Remove generated test clutter from the repository**

Ensure tracked or untracked `.DS_Store` and `__pycache__` entries are ignored by `.gitignore`; do not delete generated data outside the checkout. Verify with:

```bash
git ls-files | rg '(^|/)(\\.DS_Store|__pycache__)'
```

Expected: no output (the search exits 1 because no tracked generated clutter
matches).

- [ ] **Step 5: Run all reorganized tests**

Run: `uv run pytest tests/unit tests/contracts tests/integration -q`

Expected: all tests pass with zero skips.

- [ ] **Step 6: Commit**

```bash
git add tests .gitignore
git commit -m "test: mirror domain package organization"
```

### Task 9: Replace mypy fully with ty under uv

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Inspect and modify: active files returned by `rg -l "uv run mypy" .github`
- Modify: active root/package/development documentation
- Create/modify: `tests/contracts/test_toolchain_contract.py`

- [ ] **Step 1: Write the failing toolchain contract**

```python
from pathlib import Path

import tomllib


def test_ty_fully_replaces_mypy() -> None:
    project_root = Path(__file__).parents[2]
    pyproject_text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    config = tomllib.loads(pyproject_text)
    dev = config["dependency-groups"]["dev"]
    assert any(item.split("=", 1)[0].strip("<>!") == "ty" for item in dev)
    assert all(not item.startswith("mypy") for item in dev)
    assert "mypy" not in config["tool"]
    assert config["tool"]["ty"]["environment"]["python-version"] == "3.12"
```

Add an active-document scan limited to `README.md`, `src/**/README.md`, `docs/architecture.md`, `docs/development.md`, and workflow files; exclude historical `docs/superpowers`.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/contracts/test_toolchain_contract.py -q`

Expected: FAIL because mypy is still configured and ty is absent.

- [ ] **Step 3: Change the uv dependency set**

Run:

```bash
uv remove --dev mypy
uv add --dev ty
```

Expected: `pyproject.toml` and `uv.lock` remove mypy and add a pinned compatible ty resolution.

- [ ] **Step 4: Replace type-checker configuration**

Remove `[tool.mypy]` and all mypy overrides. Add:

```toml
[tool.ty.environment]
python-version = "3.12"
root = ["./src"]
```

Add only narrowly justified ty rules if the first check reports a concrete issue. Do not add blanket ignores.

- [ ] **Step 5: Run ty and fix diagnostics in canonical code**

Run: `uv run ty check`

Expected: initially report any migration-specific type issues. Fix annotations/imports at their canonical definitions, rerun until output ends successfully with zero diagnostics. Do not edit shims to hide canonical type errors.

- [ ] **Step 6: Update active commands and automation**

Replace active `uv run mypy` commands with `uv run ty check`. Preserve historical specifications and plans. Update `docs/development.md` to list `uv sync`, `uv lock --check`, both Ruff checks, ty, pytest coverage, and `uv build`.

- [ ] **Step 7: Verify the toolchain contract**

Run:

```bash
uv lock --check
uv run ty check
uv run pytest tests/contracts/test_toolchain_contract.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock README.md src docs/development.md \
  docs/architecture.md tests/contracts/test_toolchain_contract.py
git commit -m "chore: replace mypy with ty"
```

There are no active `.github` workflow files in the repository at plan-writing
time. If Task 9's search later finds newly added workflow files, stage those
exact returned paths in the same commit.

### Task 10: Finalize public documentation and wheel packaging

**Files:**
- Modify: root `README.md`
- Modify: all package `README.md` files
- Modify: `docs/architecture.md`, `docs/development.md`
- Modify: `pyproject.toml` only if wheel inclusion requires it
- Modify: `tests/contracts/test_package_layout.py`

- [ ] **Step 1: Add wheel-content assertions**

Extend the package-layout test with the final-package assertion and this
wheel-content helper. Keep actual wheel construction in the verification
command, not inside normal unit tests.

```python
import zipfile


WHEEL_PACKAGE_FILES = {
    "osm_polygon_description_tag/_data/osmium-export.json",
    "osm_polygon_description_tag/_data/dataset-card-template.md",
    "osm_polygon_description_tag/py.typed",
    "osm_polygon_description_tag/README.md",
    *{
        f"osm_polygon_description_tag/{domain}/README.md"
        for domain in CANONICAL_PACKAGES
    },
}


def assert_wheel_package_files(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert WHEEL_PACKAGE_FILES <= names


def test_all_final_canonical_packages_are_documented() -> None:
    package_root = Path(osm_polygon_description_tag.__file__).parent
    for package_name in CANONICAL_PACKAGES:
        readme = package_root / package_name / "README.md"
        assert readme.is_file()
        text = readme.read_text(encoding="utf-8")
        assert all(section in text for section in README_SECTIONS)
```

- [ ] **Step 2: Complete each README against the eight-section contract**

Document exact canonical imports and side effects. In the root README, keep the single resumable terminal command unchanged and link to `docs/architecture.md` and `docs/development.md`. Do not add generated counts or claims not derived from Parquet/manifests.

- [ ] **Step 3: Build and inspect the wheel**

Run:

```bash
uv build
uv run python -c "from pathlib import Path; import zipfile; wheel=max(Path('dist').glob('*.whl'), key=lambda p:p.stat().st_mtime_ns); names=set(zipfile.ZipFile(wheel).namelist()); required={'osm_polygon_description_tag/_data/osmium-export.json','osm_polygon_description_tag/_data/dataset-card-template.md','osm_polygon_description_tag/py.typed','osm_polygon_description_tag/README.md','osm_polygon_description_tag/runtime/README.md','osm_polygon_description_tag/osm/README.md','osm_polygon_description_tag/dataset/README.md','osm_polygon_description_tag/publication/README.md','osm_polygon_description_tag/workflow/README.md'}; missing=required-names; assert not missing, missing"
```

Expected: build succeeds and the assertion prints nothing.

- [ ] **Step 4: Run documentation/layout contracts**

Run: `uv run pytest tests/contracts/test_package_layout.py tests/contracts/test_toolchain_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md pyproject.toml src/osm_polygon_description_tag \
  docs/architecture.md docs/development.md tests/contracts/test_package_layout.py
git commit -m "docs: document canonical package architecture"
```

### Task 11: Full behavior-preserving verification

**Files:**
- Modify only files required by concrete verification failures

- [ ] **Step 1: Record external-directory integrity before verification**

Run:

```bash
stat -f '%N|%m|%z' \
  "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
```

Expected: two baseline records. Save them in the handoff notes, not the repository.

- [ ] **Step 2: Run formatting, linting, types, and lock checks**

Run:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run ty check
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the complete test and coverage gate**

Run:

```bash
uv run pytest --cov=osm_polygon_description_tag \
  --cov-report=term-missing --cov-fail-under=90
```

Expected: all tests pass, zero skips, and coverage is at least 90 percent.

- [ ] **Step 4: Rebuild and smoke-test the installed CLI**

Run:

```bash
uv build
uv run osm-polygon-description-tag --help
uv run osm-polygon-description-tag run-and-publish --help
```

Expected: wheel/sdist build succeeds and both help commands exit 0 with the documented subcommands/options.

- [ ] **Step 5: Verify external roots and Git hygiene**

Run the same `stat` command from Step 1 and compare exact output. Then run:

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -12
```

Expected: external root records match, diff check is clean, and only intentional commits are ahead of `origin/main`.

- [ ] **Step 6: Stop before pipeline execution or publication**

Report exact commands and outcomes, commit SHAs, compatibility coverage, ty replacement evidence, wheel contents, external-root comparison, and `git status`. Do not run a real PBF, mutate the Seagate data root, contact Hugging Face, push Git, or resume the production pipeline without a separate explicit authorization.
