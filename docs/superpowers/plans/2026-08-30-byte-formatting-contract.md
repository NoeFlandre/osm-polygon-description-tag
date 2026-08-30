# Byte Formatting Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the private dataset-card byte formatter by separating byte scaling from string rendering while preserving every existing output.

**Architecture:** Keep `_fmt_bytes` as the formatting boundary and introduce one private pure helper, `_scale_bytes`, that returns the scaled numeric value and capped binary-unit index. The formatter will retain the current byte precision, binary units, negative-value handling, and TiB cap; no public API or generated output changes.

**Tech Stack:** Python 3.12, pytest, Ruff, ty, Radon/CRAP, mutmut, MkDocs, uv.

---

### Task 1: Make the byte-scaling contract executable

**Files:**
- Modify: `tests/unit/dataset/test_deterministic_docs.py:256-268`
- Test: `tests/unit/dataset/test_deterministic_docs.py`

- [ ] **Step 1: Write the failing tests first**

Add a focused test for the new private scaling boundary alongside the existing output-format tests:

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, (0.0, 0)),
        (1_024, (1.0, 1)),
        (1_024**4, (1.0, 4)),
        (1_024**5, (1_024.0, 4)),
    ],
)
def test_scale_bytes_uses_binary_units_and_caps_at_tib(
    value: int, expected: tuple[float, int]
) -> None:
    assert docs_module._scale_bytes(value) == expected
```

The existing `test_format_bytes_uses_stable_binary_units` and
`test_format_bytes_handles_values_above_the_last_named_unit` remain as output-level
regression coverage.

- [ ] **Step 2: Run the new tests and verify the expected RED state**

Run:

```bash
env MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_deterministic_docs.py::test_scale_bytes_uses_binary_units_and_caps_at_tib
```

Expected: collection fails with `AttributeError` because `_scale_bytes` does not yet exist.

### Task 2: Isolate scaling and simplify rendering

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/docs.py:42-120`
- Test: `tests/unit/dataset/test_deterministic_docs.py`

- [ ] **Step 1: Implement the minimal helper**

Define the binary unit tuple and pure scaling helper:

```python
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _scale_bytes(value: int) -> tuple[float, int]:
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(_BYTE_UNITS) - 1:
        size /= 1024
        unit_index += 1
    return size, unit_index
```

Refactor `_fmt_bytes` to format that result without an unreachable fallback:

```python
def _fmt_bytes(value: int) -> str:
    size, unit_index = _scale_bytes(value)
    decimals = 0 if unit_index == 0 else 1
    return f"{size:,.{decimals}f} {_BYTE_UNITS[unit_index]}"
```

- [ ] **Step 2: Run focused tests and verify GREEN**

Run:

```bash
env MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_deterministic_docs.py::test_scale_bytes_uses_binary_units_and_caps_at_tib \
  tests/unit/dataset/test_deterministic_docs.py::test_format_bytes_uses_stable_binary_units \
  tests/unit/dataset/test_docs_helpers.py::test_format_bytes_handles_values_above_the_last_named_unit
```

Expected: all selected tests pass and every existing byte-format output remains unchanged.

- [ ] **Step 3: Refactor only after GREEN**

Keep `_scale_bytes` private and deterministic. Do not alter `_fmt_bytes` callers, generated Markdown, unit labels, precision, or supported input type.

### Task 3: Validate, commit, and publish

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/docs.py`
- Modify: `tests/unit/dataset/test_deterministic_docs.py`
- Create: `docs/superpowers/plans/2026-08-30-byte-formatting-contract.md`

- [ ] **Step 1: Run the repository quality gates**

Run the focused module tests, full test/coverage/CRAP gate, mutation gate, static checks,
strict documentation build, package build, and full `just check` with the isolated writable
cache directories already used by this checkout. Expected results: zero test failures,
CRAP below 6 for every function, 100% mutation score, clean lint/type/static checks,
successful docs/package builds, and no generated artifacts left in the worktree.

- [ ] **Step 2: Review the exact diff and commit**

```bash
git diff --check
git diff --stat
git diff -- src/osm_polygon_description_tag/dataset/docs.py tests/unit/dataset/test_deterministic_docs.py
git add docs/superpowers/plans/2026-08-30-byte-formatting-contract.md \
  src/osm_polygon_description_tag/dataset/docs.py \
  tests/unit/dataset/test_deterministic_docs.py
git commit -m "refactor: isolate byte scaling"
```

- [ ] **Step 3: Push and verify the exact remote commit**

```bash
git push origin codex/code-quality-refactor
git status --short --branch
git rev-parse HEAD
git ls-remote origin refs/heads/codex/code-quality-refactor
```

Expected: the worktree is clean and the local and remote commit IDs match exactly.
