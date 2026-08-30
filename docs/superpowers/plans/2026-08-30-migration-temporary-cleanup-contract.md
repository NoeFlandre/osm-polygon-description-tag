# Migration Temporary Cleanup Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the sole uncovered `_migrate_parquet` failure path by proving that a partially written temporary Parquet is deleted when migration fails.

**Architecture:** Add one focused regression test around the existing private migration boundary. The test captures the generated temporary path, creates a partial file, raises the existing `StorageError`, and verifies both error translation and cleanup; production code, public APIs, generated outputs, and runtime behavior remain unchanged.

**Tech Stack:** Python 3.12, pytest, PyArrow, coverage.py, Radon/CRAP, mutmut, Ruff, ty, MkDocs, uv.

---

### Task 1: Establish the uncovered cleanup path

**Files:**
- Read: `reports/coverage.json`
- Read: `src/osm_polygon_description_tag/dataset/migration.py:46-62`

- [ ] **Step 1: Verify the current function-level coverage assertion fails**

Run:

```bash
jq -e \
  '.files["src/osm_polygon_description_tag/dataset/migration.py"]
    .functions["_migrate_parquet"].summary.percent_covered == 100' \
  reports/coverage.json
```

Expected: exit 1 with `false`; line 62 (`temporary.unlink()`) is the only missing line in `_migrate_parquet`.

### Task 2: Protect partial-file cleanup

**Files:**
- Modify: `tests/unit/dataset/test_migration.py:266-280`
- Test: `tests/unit/dataset/test_migration.py`

- [ ] **Step 1: Add the focused regression test**

Place this test after the existing storage-error translation test:

```python
def test_migrate_parquet_removes_partial_temporary_file_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.parquet"
    _write_legacy_parquet(path)
    temporary_paths: list[Path] = []

    def fail(_reader: pq.ParquetFile, temporary: Path, _metadata: pa.Schema) -> None:
        temporary.write_bytes(b"partial parquet")
        temporary_paths.append(temporary)
        raise StorageError("invalid migrated output")

    monkeypatch.setattr(migration, "_rewrite_legacy_parquet", fail)

    with pytest.raises(MigrationError, match=re.escape(str(path))):
        migration._migrate_parquet(path)

    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
```

This exercises existing behavior only. Do not modify `migration.py`.

- [ ] **Step 2: Run the focused test and migration module**

Run:

```bash
env MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_migration.py::test_migrate_parquet_removes_partial_temporary_file_after_failure
```

Expected: 1 passed.

Then run:

```bash
env MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q tests/unit/dataset/test_migration.py
```

Expected: the complete migration test module passes.

### Task 3: Validate, commit, and publish

**Files:**
- Create: `docs/superpowers/plans/2026-08-30-migration-temporary-cleanup-contract.md`
- Modify: `tests/unit/dataset/test_migration.py`

- [ ] **Step 1: Run the repository quality gates**

Run `just risk`, `just mutation`, and `just check` with the repository's isolated writable cache directories. Also run strict MkDocs validation outside the checkout. Expected results: zero test failures, `_migrate_parquet` at 100% coverage and CRAP 5.00, every function below CRAP 6, a 100% mutation score, clean Ruff/ty/pre-commit checks, and successful documentation/package builds.

- [ ] **Step 2: Review and stage only the test and plan**

```bash
git diff --check
git diff --stat
git diff -- tests/unit/dataset/test_migration.py
git add \
  docs/superpowers/plans/2026-08-30-migration-temporary-cleanup-contract.md \
  tests/unit/dataset/test_migration.py
git diff --cached --check
git diff --cached --name-status
```

Expected: exactly one plan file and one test file are staged; no production source file is changed.

- [ ] **Step 3: Commit, push, and verify the exact remote commit**

```bash
git commit -m "test: cover migration temporary cleanup"
git push origin codex/code-quality-refactor
git status --short --branch
git rev-parse HEAD
git ls-remote origin refs/heads/codex/code-quality-refactor
```

Expected: the worktree is clean and the local and remote commit IDs match exactly.
