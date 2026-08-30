# Basemap MultiPolygon Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate MultiPolygon outer-ring selection and pin malformed-coordinate behavior without changing the basemap renderer's outputs.

**Architecture:** `draw_landmasses` and the existing Polygon/MultiPolygon dispatch remain unchanged. A private pure `_outer_rings` iterator will own the MultiPolygon container and outer-ring validation, while `_draw_multipolygon` will only draw the selected rings. Invalid top-level coordinates, empty polygon entries, and non-list polygon entries will continue to be ignored.

**Tech Stack:** Python 3.12, `pathlib`, matplotlib, pytest, uv, Ruff, ty, Radon, mutmut, Just, and MkDocs Material.

---

### Task 1: Specify malformed MultiPolygon behavior with failing tests

**Files:**
- Modify: `tests/unit/dataset/test_geography_rendering.py`
- Test: `tests/unit/dataset/test_geography_rendering.py`

- [ ] **Step 1: Add tests for the pure outer-ring contract**

Add these tests beside the existing polygon-helper tests:

```python
def test_outer_rings_ignores_non_list_coordinates() -> None:
    assert list(basemap_module._outer_rings(([],))) == []


def test_outer_rings_skips_empty_and_non_list_polygons() -> None:
    first_ring = [(0, 0), (1, 0), (0, 1)]
    second_ring = [(2, 0), (3, 0), (2, 1)]
    coordinates = [[], (first_ring,), [second_ring]]

    assert list(basemap_module._outer_rings(coordinates)) == [second_ring]
```

The tests define a pure, directly inspectable contract for malformed and
empty MultiPolygon containers before the helper exists. The second test also
pins that only list-shaped polygons contribute their first (outer) ring.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_geography_rendering.py::test_outer_rings_ignores_non_list_coordinates \
  tests/unit/dataset/test_geography_rendering.py::test_outer_rings_skips_empty_and_non_list_polygons
```

Expected result: both tests fail with an `AttributeError` because
`_outer_rings` does not yet exist. The failure must be a test failure rather
than a collection or syntax error.

### Task 2: Implement the iterator and refactor the renderer

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/geography/basemap.py`
- Test: `tests/unit/dataset/test_geography_rendering.py`

- [ ] **Step 1: Add the minimal pure iterator**

Add `Iterator` to the `collections.abc` import and add this private helper
before `_draw_multipolygon`:

```python
def _outer_rings(coordinates: Any) -> Iterator[Any]:
    """Yield the first ring from each valid MultiPolygon member."""
    if not isinstance(coordinates, list):
        return
    for polygon in coordinates:
        if isinstance(polygon, list) and polygon:
            yield polygon[0]
```

- [ ] **Step 2: Run the new tests and verify GREEN**

Run the exact command from Task 1. Both tests must pass before changing
`_draw_multipolygon`.

- [ ] **Step 3: Delegate `_draw_multipolygon` to the iterator**

Replace its current validation loop with:

```python
def _draw_multipolygon(ax: Any, coordinates: Any) -> None:
    for ring in _outer_rings(coordinates):
        _draw_ring(ax, ring)
```

This preserves the existing behavior for non-list top-level coordinates,
empty members, non-list members, and nested empty rings while leaving
`_draw_ring` responsible for the minimum three-point check.

- [ ] **Step 4: Run the complete basemap/rendering tests**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-quality-mplconfig \
MPLBACKEND=Agg \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-quality-uv-cache \
uv run pytest -q \
  tests/unit/dataset/test_geography_rendering.py \
  tests/unit/dataset/test_geography_card.py
```

Expected result: all selected rendering and card tests pass with unchanged
patch counts, geometry handling, captions, and deterministic outputs.

### Task 3: Validate and publish the scoped quality change

**Files:**
- Modify: none beyond Tasks 1–2
- Test: all repository tests and quality gates

- [ ] **Step 1: Run static checks and the full risk gate**

Run `uv lock --check`, Ruff format/check, `uv run ty check`, Radon complexity,
changed-file pre-commit hooks, and `just risk` with the isolated writable
caches. Confirm the updated MultiPolygon function has complete coverage and
all CRAP scores remain below 6.

- [ ] **Step 2: Run mutation testing and artifact checks**

Run `just mutation`, `uv run mkdocs build --strict`, and `uv build`. Require
zero surviving or timed-out mutants, a passing docs build, and both package
artifacts building successfully.

- [ ] **Step 3: Review and commit only the scoped files**

Run `git diff --check`, inspect the staged patch, and commit with:

```bash
git commit -m "refactor: isolate basemap outer-ring selection"
```

- [ ] **Step 4: Push and verify the remote commit**

Push `codex/code-quality-refactor` and verify that `git rev-parse HEAD` equals
the SHA returned by `git ls-remote origin refs/heads/codex/code-quality-refactor`.
