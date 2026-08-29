# COPY Parser and Transform Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce real-data PBF-to-GeoParquet build time by optimizing the Python COPY parser and tag-extraction hot loop while preserving all output and workflow contracts.

**Architecture:** Keep `osmium` as the streaming area-assembly boundary and retain the existing `ExportRecord` and transform interfaces. Remove avoidable Python allocations in COPY decoding and sort only matching localized tags; do not change concurrency, storage promotion, schema, or publication behavior.

**Tech Stack:** Python 3.12, uv, pytest, Ruff, ty, `osmium-tool`, Shapely, PyArrow, GeoParquet.

---

### Task 1: Add failing parser performance-contract tests

**Files:**
- Modify: `tests/unit/osm/test_extraction_helpers.py` after `_copy_unescape_with_deadline`

- [ ] **Step 1: Write the failing tests**

Add these tests after `_copy_unescape_with_deadline`:

```python
def test_copy_unescape_returns_plain_bytes_without_copying() -> None:
    wire = b"plain-wire-" * 20

    assert extraction._copy_unescape(wire) is wire


def test_parse_tags_passes_unescaped_bytes_to_json_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def loads(value: object) -> object:
        seen.append(value)
        return {"description": "value"}

    monkeypatch.setattr(extraction.json, "loads", loads)

    assert extraction._parse_tags(b'{"description":"value"}') == {"description": "value"}
    assert seen == [b'{"description":"value"}']
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py::test_copy_unescape_returns_plain_bytes_without_copying tests/unit/osm/test_extraction_helpers.py::test_parse_tags_passes_unescaped_bytes_to_json_loader -q
```

Expected: both tests fail against the current implementation because `_copy_unescape` allocates a new bytes object and `_parse_tags` passes a decoded string to `json.loads`.

### Task 2: Implement the parser fast paths

**Files:**
- Modify: `src/osm_polygon_description_tag/osm/extraction.py:79-133`
- Test: `tests/unit/osm/test_extraction_helpers.py`

- [ ] **Step 1: Add the module-level escape mapping**

Place this immediately below `STDERR_CAP_BYTES`:

```python
_COPY_ESCAPES = {
    ord("t"): 0x09,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
}
```

- [ ] **Step 2: Add the no-escape path and reuse the mapping**

At the start of `_copy_unescape`, return `data` when `b"\\" not in data`. Replace the per-iteration mapping literal with `_COPY_ESCAPES.get(nxt)`. Leave unknown and trailing backslashes handled exactly as they are now.

- [ ] **Step 3: Parse JSON from bytes**

In `_parse_tags`, replace:

```python
parsed = json.loads(_copy_unescape(field).decode("utf-8"))
```

with:

```python
parsed = json.loads(_copy_unescape(field))
```

Keep null handling, object validation, string conversion, and the existing exception behavior unchanged.

- [ ] **Step 4: Run the parser tests to verify GREEN**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py -q
```

Expected: all extraction-helper tests pass.

### Task 3: Add a failing localized-tag sorting contract

**Files:**
- Modify: `tests/unit/dataset/test_transform.py` after `test_descriptions_whitespace_only_base_becomes_none`

- [ ] **Step 1: Write the failing test**

Add this test:

```python
def test_localized_values_sorts_only_matching_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[tuple[str, str]]] = []
    real_sorted = sorted

    def record_sorted(values: object) -> list[tuple[str, str]]:
        items = list(values)  # type: ignore[arg-type]
        calls.append(items)
        return real_sorted(items)

    monkeypatch.setattr(
        "osm_polygon_description_tag.dataset.transform.sorted",
        record_sorted,
        raising=False,
    )
    tags = {
        "z": "unrelated",
        "description:fr": "FR",
        "a": "unrelated",
        "description:en": "EN",
    }

    assert descriptions_from_tags(tags) == (None, {"en": "EN", "fr": "FR"})
    assert calls == [[("en", "EN"), ("fr", "FR")]]
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/dataset/test_transform.py::test_localized_values_sorts_only_matching_pairs -q
```

Expected: the test fails because the current implementation sends all tags to `sorted`.

### Task 4: Implement matching-only localized sorting

**Files:**
- Modify: `src/osm_polygon_description_tag/dataset/transform.py:63-69`
- Test: `tests/unit/dataset/test_transform.py`

- [ ] **Step 1: Gather only matching values before sorting**

Replace `_localized_values` with:

```python
def _localized_values(tags: dict[str, str], prefix: str) -> dict[str, str]:
    marker = f"{prefix}:"
    matches = [
        (key.removeprefix(marker), value)
        for key, value in tags.items()
        if key.startswith(marker) and key != marker and value.strip()
    ]
    return dict(sorted(matches))
```

This preserves exact suffix filtering and deterministic key order while avoiding a full-tag sort for records without localized values.

- [ ] **Step 2: Run transform tests to verify GREEN**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/dataset/test_transform.py -q
```

Expected: all transform tests pass.

### Task 5: Run focused regression and static checks

**Files:**
- No additional files

- [ ] **Step 1: Run the combined focused tests**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py tests/unit/dataset/test_transform.py -q
```

Expected: all focused extraction and transform tests pass.

- [ ] **Step 2: Run formatting, linting, and typing**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run ruff format --check .
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run ruff check .
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run ty check
```

Expected: formatting, Ruff, and ty all pass with no diagnostics.

### Task 6: Verify full regression and real-data equivalence/performance

**Files:**
- No additional files

- [ ] **Step 1: Run the complete repository gate**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
just check
```

Expected: pre-commit, Ruff, ty, the full pytest coverage gate, and `uv build` pass. If only the build cannot resolve PyPI inside the sandbox, rerun the same `uv build` with network access and record that environmental retry explicitly.

- [ ] **Step 2: Build the optimized Afghanistan shard into a fresh temporary root**

Run this command and retain its JSON output and `/usr/bin/time -l` result:

```bash
optimized_root=$(mktemp -d /private/tmp/osm-polygon-description-tag-perf-optimized.XXXXXX)
/usr/bin/time -l env \
  MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
  UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
  uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf \
    --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
    --data-root "$optimized_root" \
    --osmium osmium
```

Expected: the build succeeds, reports 2,147,728 emitted features, 184 included rows, and fewer than 347.87 seconds (a 20% improvement over the 434.84-second baseline).

- [ ] **Step 3: Compare stable output values and identities**

Use the existing baseline root `/private/tmp/osm-polygon-description-tag-perf-baseline.ICeGV8` and the printed `optimized_root`:

```bash
shasum -a 256 \
  /private/tmp/osm-polygon-description-tag-perf-baseline.ICeGV8/data/afghanistan-latest.parquet \
  "$optimized_root/data/afghanistan-latest.parquet"
```

Expected: both Parquet files have the same SHA-256. Compare the two JSON results and manifests for identical emitted/included/rejection counts, source/output identities, schema/transform/area-policy revisions, dependency versions, and artifact metadata; only `code_revision` and run timestamps may differ.

- [ ] **Step 4: Confirm scope and whitespace cleanliness**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and only the approved implementation/test files plus the already committed specification/plan are present. The immutable raw PBF root and generated-data root remain untouched.

- [ ] **Step 5: Commit the implementation**

Run:

```bash
git add src/osm_polygon_description_tag/osm/extraction.py \
  src/osm_polygon_description_tag/dataset/transform.py \
  tests/unit/osm/test_extraction_helpers.py \
  tests/unit/dataset/test_transform.py
git commit -m "perf: optimize PBF record parsing hot loop"
```

Expected: a Conventional Commit is created only after all regression, equivalence, and performance checks pass.
