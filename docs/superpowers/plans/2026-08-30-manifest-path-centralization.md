# Manifest Path Centralization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace repeated Parquet-to-manifest path calculations with one private, behavior-preserving helper.

**Architecture:** `dataset.manifest` will own the filename policy because manifests already define the dataset artifact contract. The helper will perform only deterministic `Path` construction, without filesystem access or new public exports. Workflow, publication, storage, statistics, migration, deduplication, and finalization callers will delegate to it while retaining their current function signatures and error handling.

**Tech Stack:** Python 3.12, `pathlib`, pytest, uv, Ruff, ty, Radon, mutmut, Just, and MkDocs Material.

---

### Task 1: Specify the canonical manifest-path behavior

**Files:**
- Modify: `tests/unit/dataset/test_manifest.py`
- Test: `tests/unit/dataset/test_manifest.py`

- [ ] **Step 1: Write the failing test**

Add this test to the manifest unit tests:

```python
def test_manifest_path_for_uses_parquet_name_without_extension() -> None:
    assert _manifest_path_for(
        "region.latest.parquet",
        Path("generated"),
    ) == Path("generated/manifests/region.latest.manifest.json")
```

The test uses `Path` from the file's existing imports. It checks the exact
established naming policy and does not create files.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q tests/unit/dataset/test_manifest.py::test_manifest_path_for_uses_parquet_name_without_extension
```

Expected result: collection/test failure because `_manifest_path_for` does not yet exist.

### Task 2: Implement and refactor to the canonical helper

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/manifest.py`
- Modify: `src/osm_polygon_description_tag/workflow/artifacts.py`
- Modify: `src/osm_polygon_description_tag/dataset/deduplication.py`
- Modify: `src/osm_polygon_description_tag/dataset/migration.py`
- Modify: `src/osm_polygon_description_tag/dataset/stats.py`
- Modify: `src/osm_polygon_description_tag/dataset/storage.py`
- Modify: `src/osm_polygon_description_tag/workflow/finalization.py`
- Modify: `src/osm_polygon_description_tag/publication/planning.py`
- Test: `tests/unit/dataset/test_manifest.py`

- [ ] **Step 1: Write the minimal implementation**

Add this private helper immediately before `__all__` in `dataset/manifest.py`:

```python
def _manifest_path_for(output_name: str, data_root: Path) -> Path:
    """Return the manifest path paired with ``output_name`` under ``data_root``."""
    stem = output_name.removesuffix(".parquet")
    return data_root / "manifests" / f"{stem}.manifest.json"
```

Do not add it to `__all__` or compatibility shims. Import it only inside production modules that currently reconstruct the same path.

- [ ] **Step 2: Run the focused test to verify GREEN**

Run the same command from Task 1. Expected result: one passing test.

- [ ] **Step 3: Replace duplicated production formulas**

Use the helper without changing surrounding validation, ordering, error messages, or function signatures:

```python
# workflow/artifacts.py
output_path = paths.data_root / "data" / source.output_name
return output_path, _manifest_path_for(source.output_name, paths.data_root)

# callers that already receive manifests_dir
manifest_path = _manifest_path_for(parquet.name, manifests_dir.parent)

# callers that already receive data_root
manifest_path = _manifest_path_for(parquet.name, data_root)
```

Apply those forms in `deduplication._read_manifests`, `migration.migrate_dataset_schema`, `stats._validate_artifact`, `storage._validate_manifest_pair`, `workflow.finalization._inspect_artifact`, and the two relevant publication-planning paths. Keep the workflow helper's existing source-root boundary checks and directory creation unchanged.

- [ ] **Step 4: Run focused regression tests**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_manifest.py \
  tests/unit/dataset/test_deduplication_helpers.py \
  tests/unit/dataset/test_migration.py \
  tests/unit/dataset/test_storage_validation_helpers.py \
  tests/unit/publication/test_per_pbf_plan.py \
  tests/unit/workflow/test_artifacts.py \
  tests/unit/workflow/test_build_helpers.py \
  tests/unit/workflow/test_orchestrator_helpers.py \
  tests/unit/workflow/test_source_runner.py
```

Expected result: all selected tests pass with no changed outputs.

### Task 3: Validate, publish, and retain evidence

**Files:**
- Modify: none beyond Task 2
- Test: all repository tests and quality gates

- [ ] **Step 1: Run static checks**

Run `uv lock --check`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run ty check`, `uv run radon cc src/osm_polygon_description_tag -s -n C`, and the changed-file pre-commit hooks with writable Matplotlib and uv caches.

- [ ] **Step 2: Run full risk and mutation gates**

Run the repository's `just risk` and `just mutation` recipes with isolated writable caches. Record the complete test count, coverage, CRAP result, and mutation score; reject any failure, survivor, timeout, or unchecked mutant.

- [ ] **Step 3: Build and render documentation**

Run `uv build` and `uv run mkdocs build --strict`, removing only the generated `site/` directory afterward if it is untracked.

- [ ] **Step 4: Review and commit the scoped change**

Run `git diff --check`, inspect the staged five-to-nine-file diff, and stage only the files changed for this plan. Commit with:

```bash
git commit -m "refactor: centralize manifest path derivation"
```

- [ ] **Step 5: Push and verify**

Push the current `codex/code-quality-refactor` branch and verify `git rev-parse HEAD` equals `git ls-remote origin refs/heads/codex/code-quality-refactor`.
