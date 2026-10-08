# Unapplied card-wrapper patches (issue #138)

Nothing here is applied to any branch. `main` is not changed.

| File | Purpose | Applies to |
|---|---|---|
| `card-wrapper-removal.patch` | Removes `_atomic_write_template` from `dataset/geography/card.py`, and makes three edits to `tests/unit/dataset/test_geography_card.py`. | Code branch `handoff/second-master/issue-138-code-d7f63f1` (commit `d7f63f1`). Also checked on PR #145's tree. |
| `card-wrapper-removal.alt-without-contracts-file.patch` | Alternative if `tests/unit/runtime/test_atomic_wrapper_contracts.py` is absent. Same card.py change. In the test file it keeps the deleted test, repoints its call to `atomic_write_text`, and adds one import. | Same base. |

## Dependency on `tests/unit/runtime/test_atomic_wrapper_contracts.py`

The primary patch deletes `test_atomic_write_template_writes_utf8_bytes_and_leaves_no_temp_file` (test edit 3). That test checked UTF-8 bytes and no leftover temp file for the wrapper. The same behaviour is pinned in `tests/unit/runtime/test_atomic_wrapper_contracts.py`, which exists only on the code branch.

- If you have that file: apply `card-wrapper-removal.patch`.
- If you do not have that file: apply `card-wrapper-removal.alt-without-contracts-file.patch` instead. Do not apply the primary patch, or you lose the temp-file check.

## Edits in the primary patch

1. `card.py` around line 164: `_atomic_write_template(template_path, new_text)` becomes `atomic_write_text(template_path, new_text)`.
2. `card.py` around lines 192-195: the wrapper definition is deleted.
3. `test_geography_card.py` line 33: the import name `_atomic_write_template` is removed.
4. `test_geography_card.py` line 711: `patch.object(card_module, "_atomic_write_template")` becomes `patch.object(card_module, "atomic_write_text")`. Assertions unchanged.
5. `test_geography_card.py` lines 737-748: the test is deleted (see dependency above).

## Overlap with PR #145

PR #145 changes `test_geography_card.py` at base lines 22-23 (adds an import), 38 (removes the reporting import), and 233 (a comment). None of these is an edit line of this patch. But the import edit at line 33 sits 5 lines above PR #145's hunk at line 38. Expect a context merge in that import block, and keep both sides.

## Verification (on fresh clones, not on any owner's checkout)

- Primary patch: `git apply --check` passes on `d7f63f1`. After applying, `tests/unit/dataset/test_geography_card.py`, `tests/unit/dataset/test_docs_helpers.py` and `tests/unit/runtime/test_atomic_wrapper_contracts.py` give 121 passed, 0 failed.
- Alternative patch: `git apply --check` passes on a clean `d7f63f1` clone. After applying, ruff check and ruff format --check pass on both files, and `tests/unit/dataset/test_geography_card.py` gives 48 passed, 0 failed.
