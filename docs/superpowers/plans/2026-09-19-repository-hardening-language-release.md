# Repository hardening and language release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the repository into cohesive, tested modules, resolve all currently open repository issues, and run and publish the approved language-detection rerun end to end.

**Architecture:** Preserve existing behavior through compatibility re-exports and contract tests while splitting the Grid operator, language CLI, and publication API by responsibility. Keep the Grid run resumable and identity-bound, validate the complete output before the one authorized HF replacement, and verify the remote result independently.

**Tech Stack:** Python 3.12, uv, Ruff, ty, pytest/coverage, mutmut, Radon/CRAP, MkDocs Material, GitHub Actions, Grid'5000 SSH/rsync/scheduler tooling, Hugging Face Hub CLI.

---

### Task 1: Establish the implementation baseline

**Files:**
- Read: `pyproject.toml`, `justfile`, `.github/workflows/`, `src/`, `tests/`, `docs/language-detection.md`, `docs/language-rollout-status.md`
- Write: `/private/tmp/osm-polygon-description-tag-baseline-20260919/` only

- [ ] **Step 1: Record repository and remote state.**

Run:

~~~bash
mkdir -p /private/tmp/osm-polygon-description-tag-baseline-20260919
git status --short --branch
git log --oneline --decorate -12
git ls-remote origin refs/heads/main refs/heads/feat/ungate-language-confidence
~~~

Expected: the isolated branch is clean, based on `b9a84c4`, and no source data is staged.

- [ ] **Step 2: Capture the collected-test baseline before refactoring.**

Run:

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest --collect-only -q | sed -n '/^tests\//p' > /private/tmp/osm-polygon-description-tag-baseline-20260919/pytest-nodes.txt
~~~

Expected: collection exits 0; save the final collected count and test-name list.

- [ ] **Step 3: Run focused contract baselines.**

Run:

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q \
    tests/contracts/test_import_compatibility.py \
    tests/contracts/test_grid_run_driver.py \
    tests/contracts/test_quality_gate_contract.py \
    tests/unit/dataset/languages \
    tests/unit/workflow
~~~

Expected: record the exit code and failures; do not interpret stale reports as current evidence.

- [ ] **Step 4: Commit only the plan checkpoint.**

~~~bash
git add docs/superpowers/plans/2026-09-19-repository-hardening-language-release.md
git commit -m "docs: plan repository hardening and language release"
~~~

### Task 2: Make the publication API honest

**Files:**
- Modify: `src/osm_polygon_description_tag/publication/__init__.py`
- Modify: `src/osm_polygon_description_tag/publication/planning.py`
- Modify: `src/osm_polygon_description_tag/publication/upload.py`
- Modify: `tests/contracts/test_import_compatibility.py`
- Modify: `tests/unit/publication/`

- [ ] **Step 1: Add the public-surface regression assertions.**

Assert that `publication.__all__` contains no underscore-prefixed names and that supported plan/retry functions are importable by their public names. Import genuinely internal helpers from their defining modules only where a unit test directly specifies their behavior.

- [ ] **Step 2: Run the focused contract tests and observe the intended RED state.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/contracts/test_import_compatibility.py tests/unit/publication
~~~

Expected: the new public-surface assertion fails against the current private re-exports.

- [ ] **Step 3: Promote only cross-module contracts.**

Rename `_build_metadata_only_upload_plan`, `_build_per_pbf_upload_plan`, and `_default_runner_with_retry` to public names with docstrings describing deterministic plans, retry behavior, and publication identity guarantees. Keep `_build_command`, `_classify_failure`, and `_collect_allowlisted_files` private in their defining modules and update direct tests accordingly.

- [ ] **Step 4: Remove private names from the package initializer and update callers.**

Keep all existing public imports working; update internal imports to use the defining module or the newly public name. Do not change command output or upload plans.

- [ ] **Step 5: Run focused tests and inspect the diff.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/contracts/test_import_compatibility.py tests/unit/publication
git diff --check
~~~

Expected: all focused tests pass and no private names remain in `publication.__all__`.

- [ ] **Step 6: Commit the API cleanup.**

~~~bash
git add src/osm_polygon_description_tag/publication tests/contracts/test_import_compatibility.py tests/unit/publication
git commit -m "refactor(publication): make package API explicit"
~~~

### Task 3: Split the Grid operator by cohesion

**Files:**
- Move: `src/osm_polygon_description_tag/workflow/grid_operator.py` into `src/osm_polygon_description_tag/workflow/grid_operator/`
- Create: `grid_operator/__init__.py`, `validation.py`, `intent.py`, `bundle.py`, `script.py`, `submit.py`
- Modify: `src/osm_polygon_description_tag/workflow/__init__.py` only if import compatibility requires it
- Modify: `tests/unit/workflow/`, `tests/contracts/test_dependency_direction.py`, `tests/contracts/test_import_compatibility.py`

- [ ] **Step 1: Add package import and collection contracts before moving implementation.**

Cover the existing public names imported from `workflow.grid_operator`, the dataclass field shapes, and the exact test collection list. Add one test that imports the package and asserts all documented public names remain available.

- [ ] **Step 2: Verify the new contracts fail or expose the missing package boundary.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/contracts/test_import_compatibility.py tests/unit/workflow
~~~

Expected: the package-boundary assertions fail before the move, while existing behavior tests remain the safety net.

- [ ] **Step 3: Mechanically move definitions with decorator-safe source ranges.**

Use an AST-aware temporary splitter that includes the earliest decorator line for each definition. Place scalar/path validators in `validation.py`, `SubmissionIntent` and intent validators in `intent.py`, staging/bundle dataclasses and copy functions in `bundle.py`, script/payload rendering in `script.py`, and planning/submission/resumption in `submit.py`. Keep imports explicit and acyclic; the package initializer re-exports the established public surface.

- [ ] **Step 4: Run the Grid unit and contract tests immediately.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/unit/workflow tests/contracts/test_grid_run_driver.py tests/contracts/test_dependency_direction.py
~~~

Expected: all tests pass and the collected test count is unchanged.

- [ ] **Step 5: Check module cohesion and dependency direction.**

Run:

~~~bash
wc -l src/osm_polygon_description_tag/workflow/grid_operator/*.py
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/contracts/test_dependency_direction.py
~~~

Expected: no module exceeds the issue target of approximately 500 lines, and no cycle or forbidden import appears.

- [ ] **Step 6: Commit the Grid package split.**

~~~bash
git add src/osm_polygon_description_tag/workflow tests/unit/workflow tests/contracts
git commit -m "refactor(workflow): split Grid operator by responsibility"
~~~

### Task 4: Separate language CLI parsing from workflow

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/languages/language_cli.py`
- Create or modify: `src/osm_polygon_description_tag/workflow/language_run.py`, `src/osm_polygon_description_tag/workflow/language_grid.py`
- Modify: `tests/unit/test_language_cli.py`, `tests/unit/test_language_cli_grid_contracts.py`, `tests/unit/test_language_cli_mutations.py`, `tests/contracts/test_cli_contract.py`

- [ ] **Step 1: Add direct workflow tests for the helpers currently requiring an argparse namespace.**

Cover staged-job reuse, run configuration construction, Grid submit/stage orchestration, and result serialization using typed arguments or dataclasses. Preserve the exact command names, flags, exit codes, and JSON/text output.

- [ ] **Step 2: Run the new direct tests in RED.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/unit/test_language_cli.py tests/unit/test_language_cli_grid_contracts.py tests/unit/test_language_cli_mutations.py
~~~

Expected: direct imports fail until workflow helpers exist outside the CLI module.

- [ ] **Step 3: Extract workflow helpers and leave the CLI as parser/dispatcher/reporter.**

Move filesystem, staging, checkpoint, and submission decisions into focused workflow modules. Keep parser construction and output formatting in `language_cli.py`; use typed request/result objects at the boundary instead of passing `argparse.Namespace` through the workflow.

- [ ] **Step 4: Run CLI contract and direct workflow tests.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q \
    tests/unit/test_language_cli.py \
    tests/unit/test_language_cli_grid_contracts.py \
    tests/unit/test_language_cli_mutations.py \
    tests/contracts/test_cli_contract.py
~~~

Expected: existing CLI output and exit-code contracts pass unchanged.

- [ ] **Step 5: Commit the CLI/workflow split.**

~~~bash
git add src/osm_polygon_description_tag/dataset/languages src/osm_polygon_description_tag/workflow tests/unit/test_language_cli.py tests/unit/test_language_cli_grid_contracts.py tests/unit/test_language_cli_mutations.py tests/contracts/test_cli_contract.py
git commit -m "refactor(language): separate CLI parsing from workflow"
~~~

### Task 5: Consolidate test fixtures without reducing coverage

**Files:**
- Create: `tests/unit/workflow/conftest.py`, `tests/unit/dataset/conftest.py`, and the smallest additional package-local fixture modules required by repetition analysis
- Modify: tests containing repeated run/manifests/bundle setup
- Remove: all `tempfile.mkdtemp` calls from tests

- [ ] **Step 1: Inventory repeated setup and temporary-directory calls.**

~~~bash
rg -n 'tempfile\\.mkdtemp|tmp_path|write_.*manifest|SubmissionIntent|JobBundle|language-run' tests
~~~

Group only setup repeated at least three times; do not consolidate assertions or remove test cases.

- [ ] **Step 2: Add fixture tests or fixture-backed parametrizations that preserve current names.**

Introduce package-local fixtures for the repeated workflow paths and language shard layouts. Use `tmp_path` or `tmp_path_factory` and explicit fixture scope; never use process-global mutable state.

- [ ] **Step 3: Replace `mkdtemp` and run the targeted suite.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q tests/unit/workflow tests/unit/dataset tests/unit/publication
~~~

Expected: no `mkdtemp` references remain, tests pass, and no test name was removed.

- [ ] **Step 4: Verify collection identity.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest --collect-only -q | sed -n '/^tests\//p' > /private/tmp/osm-polygon-description-tag-baseline-20260919/pytest-nodes-after-fixtures.txt
diff -u /private/tmp/osm-polygon-description-tag-baseline-20260919/pytest-nodes.txt /private/tmp/osm-polygon-description-tag-baseline-20260919/pytest-nodes-after-fixtures.txt
~~~

Expected: the diff is empty; every test node remains present.

- [ ] **Step 5: Commit fixture consolidation.**

~~~bash
git add tests
git commit -m "test: centralize workflow and dataset fixtures"
~~~

### Task 6: Close the mutation-quality gap

**Files:**
- Modify: source files identified by the mutation report
- Modify: focused tests alongside each real surviving mutant
- Modify: `scripts/run_mutation_gate.py` only when a proven harness defect is found
- Modify: `tests/contracts/test_quality_gate_contract.py` for every gate contract change

- [ ] **Step 1: Generate fresh coverage contexts and run the all-source mutation gate.**

~~~bash
mkdir -p data-root/.tmp reports
COVERAGE_FILE="$PWD/data-root/.tmp/.coverage-ctx" \
  UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run pytest -q -p no:cacheprovider --cov=osm_polygon_description_tag --cov-branch --cov-context=test --cov-report=
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run python -m scripts.run_mutation_gate --max-children 8 --coverage-file data-root/.tmp/.coverage-ctx
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run python scripts/check_mutation_score.py --mutants-root mutants --output reports/mutation-summary.json --minimum-score 100
~~~

Expected: obtain the current survivor list, not the stale September report.

- [ ] **Step 2: Classify survivors by behavior.**

For each survivor, run its covering test set and inspect the mutant diff. A real behavior change receives one focused failing test first, then the minimal production change. A mathematically equivalent or unreachable mutant is addressed only with an explicit contract or implementation simplification and recorded in the commit message.

- [ ] **Step 3: Repeat the gate until all scored mutants are killed.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run python scripts/check_mutation_score.py --mutants-root mutants --output reports/mutation-summary.json --minimum-score 100
~~~

Expected: `score=100`, `survived=0`, `timeout=0`, `suspicious=0`, and all other unresolved buckets zero.

- [ ] **Step 4: Commit mutation hardening.**

~~~bash
git add src tests scripts reports/mutation-summary.json
git commit -m "test: close surviving mutation gaps"
~~~

### Task 7: Update documentation and remove disposable repository garbage

**Files:**
- Modify: `docs/language-detection.md`
- Modify: `docs/language-rollout-status.md`
- Modify: `docs/development.md`, `docs/operations.md`, or generated card sources where stale claims are found
- Remove: only verified disposable ignored outputs such as stale coverage fragments, local caches, and obsolete mutation scratch state

- [ ] **Step 1: Replace stale rollout claims with the new run identity and explicit status.**

Document the ungated policy, current fingerprint, source snapshot, 386-shard run, final counts, model revisions, and publication revision only after those values are verified. Remove contradictory “not uploaded” or old-run “complete” wording.

- [ ] **Step 2: Add a documentation contract check for the policy and release invariants.**

Assert that the public documentation names the remaining structural uncertainty rules, the fallback order, exact split gating, and the no-op publication requirement.

- [ ] **Step 3: Inventory ignored outputs before deletion.**

~~~bash
git status --short --ignored
git ls-files dist site reports data-root | sed -n '1,260p'
~~~

Remove only generated files that are untracked/ignored, reproducible, and not source data or a release checkpoint. Re-run `git status --short --ignored` and retain an explicit inventory of anything preserved.

- [ ] **Step 4: Build and validate the documentation.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv run mkdocs build --strict
git diff --check
~~~

- [ ] **Step 5: Commit documentation and hygiene changes.**

~~~bash
git add docs
git commit -m "docs: align release status with current language policy"
~~~

### Task 8: Run the complete local quality gate and review the branch

**Files:**
- Read: all changed files and generated reports
- Modify: only files required by fresh gate failures

- [ ] **Step 1: Run the repository gate.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache just check
~~~

- [ ] **Step 2: Run risk and mutation reports from the final source.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache just risk
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache just mutation
~~~

- [ ] **Step 3: Run build and CLI smoke checks.**

~~~bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache just build
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv run osm-polygon-description-tag --help
~~~

- [ ] **Step 4: Review the final diff and commit any gate fixes.**

~~~bash
git diff --check
git status --short
git diff --stat origin/feat/ungate-language-confidence...HEAD
~~~

Expected: no unintentional data, cache, generated-site, or credential changes; worktree clean after the final commit.

### Task 9: Push, review, merge, and close GitHub items

**Files:**
- Read/write: GitHub branch, pull request, issue comments, and checks

- [ ] **Step 1: Verify GitHub authentication and push the branch.**

~~~bash
gh auth status
git push --set-upstream origin codex/repo-hardening-release
~~~

Expected: authenticated owner access, remote branch at the reviewed commit, and no force push.

- [ ] **Step 2: Open or update one reviewable pull request.**

Use a body that links #8, #9, #10, #11, #20, and #21, states the 100% mutation policy, lists quality evidence, and defers the HF publication claim until the release gate completes.

- [ ] **Step 3: Resolve CI and review findings.**

Wait for required checks, inspect failures, add tests/fixes with RED→GREEN evidence, and rerun the full local gate for each material change.

- [ ] **Step 4: Merge only after checks and review pass.**

Verify the merged commit and `main` remote SHA independently, then close each addressed issue with specific commit/check/release links. Re-list open issues and open PRs; unresolved items are a stop condition.

### Task 10: Run the language rerun on Grid'5000

**Files:**
- Read: `scripts/run_language_grid.py`, `src/osm_polygon_description_tag/dataset/languages/`, `src/osm_polygon_description_tag/dataset/sentences/`, Grid and language documentation
- Write: the exact resumable run directory selected by the driver under the Seagate data root; never write raw source files

- [ ] **Step 1: Preflight remote access and model/config identity.**

Confirm SSH access, scheduler site/quota policy, model availability, source manifest count (386), source snapshot, Lingua/GlotLID/SaT revisions and hashes, and the new policy fingerprint `1d6f31e245a922d89d6d341f24cf0db2304160b2b7ce4f5d8eb890eef148c9a7`. Resolve any prior durable unresolved intent before submitting a new job.

- [ ] **Step 2: Run the driver in resumable apply mode.**

Use the repository’s language Grid CLI with the exact source root, run root, project identifier, site allowlist, and scheduler margin documented by `scripts/run_language_grid.py`. Keep one active shard submission at a time unless the driver’s existing bounded parallelism explicitly allows otherwise. Never resubmit an ambiguous job.

- [ ] **Step 3: Monitor checkpoints and reconcile every shard.**

Require 386/386 completed shards, valid per-shard receipts/checkpoints, exact source identity, annotation conservation, complete output manifests, and no unresolved scheduler or publication intent.

- [ ] **Step 4: Run independent local validation before HF mutation.**

Recompute counts and hashes from the final output without trusting the driver’s summary. Confirm the new fingerprint is present everywhere and the output does not match the old conservative fingerprint.

### Task 11: Publish and independently verify Hugging Face output

**Files:**
- Read/write: exact language release directory and HF dataset repository `NoeFlandre/osm-polygon-description-tag`

- [ ] **Step 1: Run the publication dry-run.**

Confirm the exact allowlisted target consists of the 386 language Parquets plus deterministic stats/export metadata, with no source or scratch files.

- [ ] **Step 2: Apply the HF upload once.**

Use the repository’s language publication command and authenticated `hf` CLI. Record the returned revision and upload manifest; do not delete unrelated dataset paths.

- [ ] **Step 3: Verify the remote artifact independently.**

Use `hf datasets info` and direct remote downloads/API reads to compare file inventory, byte sizes, SHA-256 hashes, schema, counts, fingerprint, and top-language statistics against the local validated manifest.

- [ ] **Step 4: Perform the no-op rerun.**

Run the same publication command again. Require zero uploads/deletes and a report proving the remote bytes and revision remain unchanged.

- [ ] **Step 5: Record the final release evidence and close the release issue/PR.**

Update the rollout status with the actual run ID, counts, Grid completion, HF revision, remote verification, and no-op result. Re-run GitHub issue/PR inventory and stop if any addressed issue remains open.
