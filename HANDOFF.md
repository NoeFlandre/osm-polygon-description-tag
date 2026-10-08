# HANDOFF: issue #138, osm-polygon-description-tag

**Status: PARTIAL. Issue #138 stays open.** Nothing is merged. `main` is not changed. No PR is open. Nothing is posted to any issue.

## 1. Branches

| Branch | Commit | What it is |
|---|---|---|
| `handoff/second-master/issue-138-code-d7f63f1` | `d7f63f11df9352171b3559c33b880cafc43ad029` | The code. Five commits on top of `ea22740443e526836590c267afa8ec87db2c222d`. |
| `handoff/second-master/issue-138-notes-d7f63f1` | Orphan branch; this file is at its root. Full SHA: see the commit link returned with this handoff. | Notes only. No code. Evidence, reproducer, issue draft, unapplied patches, and copies of the scratch-worktree files. |

The code branch head is verified to equal `d7f63f1`. Commit list on the code branch: `5c7f94f`, `cc395b9`, `40952cd`, `13ace50`, `d7f63f1`.

## 2. Restore from GitHub (no attachments needed)

```
git clone --branch handoff/second-master/issue-138-code-d7f63f1 https://github.com/NoeFlandre/osm-polygon-description-tag code
git -C code rev-parse HEAD        # must print d7f63f11df9352171b3559c33b880cafc43ad029
git -C code log --oneline ea22740443e526836590c267afa8ec87db2c222d..HEAD

git clone --branch handoff/second-master/issue-138-notes-d7f63f1 https://github.com/NoeFlandre/osm-polygon-description-tag notes
```

Setup for tests: `uv sync --frozen` in `code`, then the commands in section 6. No model is needed.

## 3. What changed (code branch)

- `src/osm_polygon_description_tag/dataset/docs.py`: removed `_write_if_changed`, a thin UTF-8 wrapper around `_atomic_write_if_changed`. Its two call sites now encode the text and call `_atomic_write_if_changed`, inside the same `no mutate` pragma. `_atomic_write_if_changed` is kept, because it has the no-op check.
- `tests/unit/dataset/test_docs_helpers.py`, `tests/unit/dataset/test_reporting_helpers.py`: updated references. Exact-byte checks now pass through the production path.
- New: `tests/unit/runtime/test_atomic_wrapper_contracts.py`. Characterization tests for bytes, UTF-8, no-op inode and mtime, failure cleanup, and permissions.

Not changed on the branch: `dataset/geography/card.py`, `publication/state.py`, `tests/unit/dataset/test_geography_card.py`, and PR #168's files.

## 4. Not done, and why

1. **`card.py` wrapper removal (`_atomic_write_template`): blocked.** It needs three edits in `tests/unit/dataset/test_geography_card.py`, which open PR #145 changes. The patch is ready as an unapplied file: `patches/card-wrapper-removal.patch`. See section 5 for its dependency.
2. **`publication/state.py` `_atomic_write_json`: kept.** It writes pretty JSON (`indent=2`, `sort_keys`, `ensure_ascii=False`, trailing newline). The shared `atomic_write_json` writes compact canonical JSON. They are not interchangeable without changing bytes.
3. **CRLF newline behaviour: not decided.** See section 8.

## 5. Unapplied card patch

Files: `patches/card-wrapper-removal.patch` (primary), `patches/card-wrapper-removal.alt-without-contracts-file.patch` (alternative), `patches/README.md` (full notes).

- **Dependency.** The primary patch deletes a test whose check (UTF-8 bytes, no leftover temp file) is pinned in `tests/unit/runtime/test_atomic_wrapper_contracts.py`. That file exists only on the code branch.
- **If you have that file:** apply the primary patch.
- **If you do not:** apply the alternative. It keeps the test, repoints it to `atomic_write_text`, and adds one import. Do not apply the primary patch without that file.
- Both apply to `d7f63f1` with `git apply`. Verified on fresh clones (apply check, then focused tests: primary 121 passed; alternative 48 passed in `test_geography_card.py`, with ruff clean).
- PR #145 overlap: its hunks are at base lines 22-23, 38 and 233. None touches an edit line of the patch. The import edit at line 33 sits 5 lines above its hunk at line 38, so expect a context merge in that import block.

## 6. Checks on `d7f63f1`

Full table: `evidence/checks.md`.

| Check | Result |
|---|---|
| Unit + contracts (acceptance and integration excluded) | PASS, 4150 passed, 5 skipped, exit 0 |
| Acceptance + integration | PASS, 23 passed, 6 skipped, exit 0 |
| Coverage (unit + contracts run) | 99.13% |
| Focused, 4 files | PASS, 90 passed |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS, 371 files |
| `ty check` | PASS |
| CRAP, `check --max-crap-score 6` | PASS, 1716 functions, max repo 5.47; docs.py changed functions 3.0, 3.0, 1.0 |
| Mutation, `dataset/docs.py` only | PASS, 924 of 924 killed, gate 100% |

**Mutation scope caveat.** The 924/924 result covers `dataset/docs.py` only, with `tests/unit` and `tests/contracts` as the test selection. It is not a repo-wide mutation result. The repo-wide `just mutation` recipe was not run. Also, 425 mutants were alive in some intermediate pass and were killed later. All 425 are in the final killed list. Details: `evidence/mutation-docs-py.md`.

## 7. Skips (all of them)

Unit + contracts run (5):
- `tests/contracts/test_docker.py:144`: needs `RUN_DOCKER_SMOKE=1`.
- `tests/unit/osm/test_real_osmium_coverage.py` lines 129, 156, 216, 234: osmium binary not installed.

Acceptance + integration run (6):
- `tests/helpers/osmium.py:16`: 2 tests, osmium executable required.
- `tests/integration/test_public_cli_lifecycle.py` lines 124, 159, 274, 480: osmium binary not installed.

## 8. CRLF: what is known and what is not decided

Kept separate on purpose.

- **Byte-preservation comments exist.** Quotes are in `crlf/issue-draft.md`. They say that prose is "preserved byte-for-byte" (`card.py` lines 6-7, 109-110, 176-179; `docs.py` lines 469-470), and that the write side does not translate newlines (`runtime/atomic.py:37`). A test comment (`test_geography_card.py` lines 853-856, in PR #145's file) also says the card writer keeps CRLF. This is a stated intent.
- **Actual behaviour.** CRLF input comes out as LF on the read paths: `card.py:157` and `docs.py:475, 478` use `read_text`. Reproduced in `crlf/repro_output.txt`.
- **Not a settled policy.** The comments describe intent. No doc or test sets a newline policy for both directions. Hub reads are normalized on purpose (`publication/verification.py:239`). The code and tests disagree with the byte-preservation comments.
- **Current impact on tracked files: none found.** 0 of 440 tracked files are CRLF. The card template copies are LF. `write_map_block_marker_to_template` has no production caller. `_card_source` only receives the packaged template.
- **Pinned test.** `tests/unit/runtime/test_atomic_wrapper_contracts.py` pins the LF result with a "KNOWN DEFECT" comment. That label is ours. It rests on the comments above, and the owner has not confirmed it.
- **Draft issue:** `crlf/issue-draft.md`. It is marked "Draft. Not posted." Options (a) keep LF and document it, (b) preserve CRLF on read, (c) decide later. The owner's question is still open.

## 9. Known review nits, not fixed (all LOW)

1. Commit `cc395b9`'s message says mutation scope is unchanged. Not exact: the two write calls are now mutable. Covered by the tests. Correct it in the PR body. Do not rewrite history.
2. One runtime test uses `monkeypatch.undo()`, which also reverts an autouse guard. No effect today. Preferred: `with monkeypatch.context():`.
3. The `test_geography_card.py` comment at lines 853-856 (PR #145's file) contradicts the pinned CRLF behaviour.
4. Pinned CRLF-to-LF test (section 8).
5. Mode is checked only for a new file, not for a replace.

Full record: `evidence/review.md`.

## 10. Not run

- Repo-wide mutation (`just mutation`).
- Docker smoke test; Docker builds.
- Osmium-dependent tests (binary missing on the test machine).
- Other justfile recipes: `audit`, `bench`, benchmarks, docs build.
- Coverage and CRAP on rounds 2-4 commits were not run separately; the final head is what was measured.

## 11. Model identity and trust

Every subagent reported its model as `claude-haiku-5-5` from its own environment. Not verified from outside. Treat as self-reported.

## 12. What is not in this handoff, on purpose

Excluded: `.venv/`, all caches (`.ruff_cache`, `.hypothesis`, `__pycache__`), `mutants/` (mutmut's mutated copies), `data-root/` (contents created by the gate), coverage data (`.coverage`, `reports/coverage.json`, mutmut cache), and raw progress logs. None holds a secret. The verdicts and counts from them are recorded above.

No credentials, private paths, emails, or session identifiers are in this branch. Paths are written as placeholders such as `<repo>`. A leftover scan on this branch found no matches.

## 13. Files on this branch

- `ledger.md`: working ledger (a snapshot from before the final steps).
- `evidence/checks.md`, `evidence/review.md`, `evidence/crap-summary.md`, `evidence/mutation-docs-py.md`.
- `evidence/mutation/`: scoped run script, counts, killed names (924), intermediate names (425 unique).
- `evidence/logs/`: unit + contracts log and acceptance + integration log (sanitised).
- `crlf/`: reproducer (`repro_crlf.py`, takes `REPO_SRC`), output, issue draft (not posted).
- `patches/`: primary and alternative card patches, and their README.
- `scratch-worktree/`: the two dirty files from the scratch worktree, copies as preserved, and the diff.

## 14. Open decisions for the owner

1. Push or PR for the code branch.
2. When to apply the card patch (after PR #145 merges, or now with the alternative).
3. CRLF contract: (a), (b) or (c). And whether to post the draft issue.
4. The LOW nits in section 9.
