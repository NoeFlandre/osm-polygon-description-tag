# Checks on code head d7f63f1 (osm-polygon-description-tag)

Code branch head: `d7f63f11df9352171b3559c33b880cafc43ad029`. Base: `ea22740443e526836590c267afa8ec87db2c222d` (the then-current main; main was not changed).

Status key: PASS / FAIL / SKIP (test skipped by the test suite) / NOT RUN. Counts are copied from the run logs.

| Check | How it was run | Result | Counts / runtime | Notes |
|---|---|---|---|---|
| Unit + contracts | pytest, branch coverage, `--ignore=tests/acceptance --ignore=tests/integration` | PASS, exit 0 | 4150 passed, 5 skipped, 4 warnings; 455 s | Log: `logs/full_suite_d7f63f1.log`. Skips listed in HANDOFF.md. |
| Acceptance + integration | pytest `tests/acceptance tests/integration` | PASS, exit 0 | 23 passed, 6 skipped; 30 s | Log: `logs/acceptance_integration_d7f63f1.log`. Skips are osmium-related. |
| Coverage | `coverage json` from the unit + contracts run data | 99.13% total | 12009 of 12085 statements; 3135 of 3192 branches | Coverage data not uploaded (generated). Covers unit + contracts only, not acceptance or integration. |
| Focused: 4 files | pytest on `test_docs_helpers.py`, `test_reporting_helpers.py`, `test_state_helpers.py`, `runtime/test_atomic_wrapper_contracts.py` | PASS | 90 passed, about 1 s | Reviewer run. The round 4 run also gave 90 passed. |
| ruff check (full repo) | `ruff check .` | PASS | All checks passed | Run on d7f63f1 in the clean checkout. |
| ruff format --check (full repo) | `ruff format --check .` | PASS | 371 files already formatted | Run on d7f63f1. |
| ty check | `ty check` | PASS | All checks passed | Run on d7f63f1. |
| CRAP | `quality_metrics.py crap` then `check --max-crap-score 6` | PASS, exit 0 | 1716 functions scored, 0 unmatched | See `evidence/crap-summary.md`. |
| Mutation, docs.py only | scoped mutmut run plus `check_mutation_score` gate | PASS, 924/924 killed | 924 mutants; scope: unit + contracts tests only | NOT a repo-wide mutation result. See `evidence/mutation-docs-py.md`. |
| Card patch, apply check | `git apply --check` on a fresh clone of the code branch | PASS | | `patches/card-wrapper-removal.patch` |
| Card patch, focused tests after applying | 3 files | PASS | 121 passed | Run in a fresh clone; the clone was reset afterwards. |
| Card alternative patch | `git apply --check` on a clean clone; ruff check and format; `test_geography_card.py` | PASS | 48 passed | `patches/card-wrapper-removal.alt-without-contracts-file.patch` |
| CRLF reproducer, re-run | `crlf/repro_crlf.py` with `REPO_SRC` set to the fresh clone's `src` | PASS | Output byte-identical to `crlf/repro_output.txt` | Temp dirs are created in the system temp location. |

## Not run

- Repo-wide mutation recipe (`just mutation`). Only the docs.py scoped run was done.
- Docker smoke test (skipped by env flag `RUN_DOCKER_SMOKE`). Docker was not run.
- Osmium-dependent tests (binary not installed on the machine): 4 unit and 4 integration tests skipped, plus 2 in the synthetic end-to-end helper.
- Other justfile recipes outside the checks above were not run, including `audit`, `bench` and the benchmarks, and docker builds.
- Coverage and CRAP were not re-run on round 2-4 commits. They ran on d7f63f1 (the final head), which is the version that matters.
- `ty check` and full-repo ruff were run on the d7f63f1 checkout, not on the separate fresh clone used for patch checks.
