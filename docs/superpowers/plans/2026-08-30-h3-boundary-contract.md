# H3 Boundary Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate H3 boundary normalization and pin empty, malformed, and degenerate-ring behavior without changing the public geometry API.

**Architecture:** `cell_rings` will remain responsible for the H3 library call and invalid-cell handling. A new private, pure `_rings_from_boundary` helper will own coordinate-order conversion, antimeridian splitting, and the minimum three-point drawable-ring filter. No public exports, output formats, or rendering behavior will change.

**Tech Stack:** Python 3.12, `h3`, `pathlib`, pytest, uv, Ruff, ty, Radon, mutmut, Just, and MkDocs Material.

---

### Task 1: Specify boundary edge-case behavior with failing tests

**Files:**
- Modify: `tests/unit/dataset/test_geography_h3.py`
- Test: `tests/unit/dataset/test_geography_h3.py`

- [ ] **Step 1: Add the boundary contract tests**

Add these tests alongside the existing `cell_rings` tests:

```python
def test_cell_rings_returns_empty_for_empty_h3_boundary() -> None:
    with patch.object(h3_policy_module.h3, "cell_to_boundary", return_value=[]):
        assert cell_rings("cell") == []


def test_rings_from_boundary_ignores_short_boundary_pairs() -> None:
    boundary = [(10.0, 20.0), (30.0,), (50.0, 60.0), (70.0, 80.0)]

    assert h3_policy_module._rings_from_boundary(boundary) == [
        [(20.0, 10.0), (60.0, 50.0), (80.0, 70.0)]
    ]


def test_rings_from_boundary_discards_rings_with_fewer_than_three_points() -> None:
    assert h3_policy_module._rings_from_boundary([(10.0, 20.0), (30.0, 40.0)]) == []
```

The empty-boundary test pins the existing public contract. The two pure-helper
tests define the normalization seam before it exists; the test run must be
red because `_rings_from_boundary` is not yet defined.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_geography_h3.py::test_cell_rings_returns_empty_for_empty_h3_boundary \
  tests/unit/dataset/test_geography_h3.py::test_rings_from_boundary_ignores_short_boundary_pairs \
  tests/unit/dataset/test_geography_h3.py::test_rings_from_boundary_discards_rings_with_fewer_than_three_points
```

Expected result: the empty-boundary test passes against the existing behavior,
while the two helper tests fail because the helper is missing. The overall
command must be nonzero for the correct reason, with no collection or syntax
errors.

### Task 2: Implement the pure normalization seam and refactor the caller

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/geography/h3_policy.py`
- Test: `tests/unit/dataset/test_geography_h3.py`

- [ ] **Step 1: Add the minimal helper after `_boundary_points`**

Add:

```python
def _rings_from_boundary(
    boundary: Sequence[Sequence[float]],
) -> list[list[tuple[float, float]]]:
    """Convert an H3 boundary into drawable, antimeridian-safe rings."""
    raw_points = _boundary_points(boundary)
    return [ring for ring in split_antimeridian(raw_points) if len(ring) >= 3]
```

- [ ] **Step 2: Run the three contract tests and verify GREEN**

Run the exact command from Task 1. All three tests must pass before changing
`cell_rings`.

- [ ] **Step 3: Delegate `cell_rings` to the pure helper**

Replace only the final conversion block in `cell_rings`:

```python
    if not boundary:
        return []
    return _rings_from_boundary(boundary)
```

Keep the H3 exception tuple, invalid-cell return value, empty-boundary return,
function signature, and public export list unchanged.

- [ ] **Step 4: Run the complete H3 unit module**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q tests/unit/dataset/test_geography_h3.py
```

Expected result: all H3 policy tests pass with the existing coordinate,
antimeridian, invalid-cell, and ordering outputs preserved.

### Task 3: Validate and publish the scoped quality change

**Files:**
- Modify: none beyond Tasks 1–2
- Test: all repository tests and quality gates

- [ ] **Step 1: Run static checks and the full risk gate**

Run `uv lock --check`, Ruff format/check, `uv run ty check`, Radon complexity,
changed-file pre-commit hooks, and `just risk` with the isolated writable
caches. Record the complete test count, coverage, and CRAP result.

- [ ] **Step 2: Run mutation testing and artifact checks**

Run `just mutation`, `uv run mkdocs build --strict`, and `uv build`. Require
zero surviving or timed-out mutants, a passing docs build, and both package
artifacts building successfully.

- [ ] **Step 3: Review and commit only the scoped files**

Run `git diff --check`, inspect the staged patch, and commit with:

```bash
git commit -m "refactor: isolate H3 boundary normalization"
```

- [ ] **Step 4: Push and verify the remote commit**

Push `codex/code-quality-refactor` and verify that `git rev-parse HEAD` equals
the SHA returned by `git ls-remote origin refs/heads/codex/code-quality-refactor`.
