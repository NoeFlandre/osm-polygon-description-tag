# COPY Common-Path Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accelerate the dominant COPY parsing path with a direct JSON decoder and compatibility-preserving no-escape fast paths.

**Architecture:** Keep `stream_export`, `iter_records`, `parse_copy_record`, and `ExportRecord` interfaces unchanged. Optimize only the common path inside extraction and preserve the standard JSON decoder as a fallback for inputs outside `orjson`'s accepted subset.

**Tech Stack:** Python 3.12, uv, `orjson`, pytest, Ruff, ty, `osmium-tool`.

---

### Task 1: Add failing common-path parser tests

**Files:**
- Modify: `tests/unit/osm/test_extraction_helpers.py`

- [ ] **Step 1: Assert the fast JSON loader receives bytes**

Replace the existing loader test with:

```python
def test_parse_tags_passes_unescaped_bytes_to_json_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def loads(value: object) -> object:
        seen.append(value)
        return {"description": "value"}

    monkeypatch.setattr(
        extraction,
        "orjson",
        SimpleNamespace(loads=loads),
        raising=False,
    )

    assert extraction._parse_tags(b'{"description":"value"}') == {"description": "value"}
    assert seen == [b'{"description":"value"}']
```

The current implementation uses `extraction.json.loads`, so the recording
list remains empty and this test fails.

- [ ] **Step 2: Assert standard-library compatibility fallback**

Add:

```python
def test_parse_tags_falls_back_to_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def loads(value: object) -> object:
        seen.append(value)
        raise json.JSONDecodeError("forced fallback", "", 0)

    monkeypatch.setattr(
        extraction,
        "orjson",
        SimpleNamespace(loads=loads, JSONDecodeError=json.JSONDecodeError),
        raising=False,
    )

    assert extraction._parse_tags(b'{"value": NaN}') == {"value": "nan"}
    assert seen == [b'{"value": NaN}']
```

The current implementation returns the expected standard-library result but
never attempts the recording fast loader, so the `seen` assertion fails.

- [ ] **Step 3: Assert plain records bypass COPY unescape**

Add:

```python
def test_plain_copy_record_bypasses_unescape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_value: bytes) -> bytes:
        raise AssertionError("plain fields should not call _copy_unescape")

    monkeypatch.setattr(extraction, "_copy_unescape", fail)

    record = extraction.parse_copy_record(
        b"0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{\"highway\":\"service\"}\n"
    )

    assert record.osm_id == 42
    assert record.tags == {"highway": "service"}
```

The current parser calls `_copy_unescape` for every field, so this test fails
before the optimization.

- [ ] **Step 4: Assert normal records bypass whitespace allocation**

Add:

```python
def test_iter_records_does_not_strip_normal_records() -> None:
    class NoStripBytes(bytes):
        def strip(self, chars: bytes | None = None) -> bytes:
            raise AssertionError("normal records should not call strip")

    line = NoStripBytes(b"0103\tway\t42\t1\t1\t2026-01-01T00:00:00Z\t{}\n")

    records = list(extraction.iter_records([line]))

    assert [record.osm_id for record in records] == [42]
```

The current unconditional `raw.strip()` makes this test fail before the
optimization.

- [ ] **Step 5: Run the new tests to verify RED**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py::test_parse_tags_passes_unescaped_bytes_to_json_loader \
  tests/unit/osm/test_extraction_helpers.py::test_parse_tags_falls_back_to_stdlib \
  tests/unit/osm/test_extraction_helpers.py::test_plain_copy_record_bypasses_unescape \
  tests/unit/osm/test_extraction_helpers.py::test_iter_records_does_not_strip_normal_records -q
```

Expected: all four tests fail for the missing fast-path behavior, not due to
collection or syntax errors.

### Task 2: Make `orjson` an explicit runtime dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` through `uv lock`

- [ ] **Step 1: Add the direct dependency**

Add this entry to the `[project].dependencies` list near the other runtime
serialization dependencies:

```toml
    "orjson>=3.10,<4",
```

- [ ] **Step 2: Refresh the lockfile without upgrades**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv lock
```

Expected: the lockfile records `orjson` in the root package dependency list;
versions of unrelated packages do not change.

### Task 3: Implement compatibility-preserving extraction fast paths

**Files:**
- Modify: `src/osm_polygon_description_tag/osm/extraction.py`
- Test: `tests/unit/osm/test_extraction_helpers.py`

- [ ] **Step 1: Add the fast decoder and fallback**

Import `orjson` and retain `json`, then add:

```python
def _load_tags(payload: bytes) -> object:
    try:
        return orjson.loads(payload)
    except orjson.JSONDecodeError:
        return json.loads(payload)
```

Use `_load_tags` in `_parse_tags` while keeping its existing outer
`json.JSONDecodeError` handling and key/value string normalization.

- [ ] **Step 2: Avoid unescape on plain scalar fields**

Use these common-path branches while retaining the existing escaped branches:

```python
def _nullable_int(field: bytes) -> int | None:
    if field == b"\\N":
        return None
    if b"\\" not in field and field.isascii():
        return int(field)
    return int(_copy_unescape(field).decode("utf-8"))


def _nullable_str(field: bytes) -> str | None:
    if field == b"\\N":
        return None
    if b"\\" not in field:
        return field.decode("utf-8")
    return _copy_unescape(field).decode("utf-8")
```

Apply the same conditional expression in `parse_copy_record` for geometry,
OSM type, and OSM ID; use `int(osm_id)` only when `b"\\" not in osm_id and
osm_id.isascii()`.

- [ ] **Step 3: Avoid unescape call overhead for plain JSON**

In `_parse_tags`, use:

```python
payload = field if b"\\" not in field else _copy_unescape(field)
parsed = _load_tags(payload)
```

This passes plain JSON bytes directly and retains the existing COPY escape
decoder for escaped JSON.

- [ ] **Step 4: Avoid normal-line whitespace allocation**

In `iter_records`, preserve the empty and whitespace-only skip semantics with:

```python
for line_number, raw in enumerate(stream, start=1):
    if not raw:
        continue
    if raw[0] in (0x20, 0x09, 0x0D, 0x0A) and not raw.strip():
        continue
    try:
        yield parse_copy_record(raw)
    except ValueError as error:
        raise ValueError(f"invalid COPY record on line {line_number}: {error}") from error
```

Normal records proceed directly to `parse_copy_record`.

- [ ] **Step 5: Run the focused tests to verify GREEN**

Run:

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py -q
```

Expected: all extraction-helper tests pass, including escaped, malformed, null,
Unicode, and blank-line coverage.

### Task 4: Run repository verification

**Files:**
- No additional files

- [ ] **Step 1: Run focused regression tests**

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
uv run pytest tests/unit/osm/test_extraction_helpers.py tests/unit/osm/test_extraction.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run formatting, linting, and typing**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv run ruff format --check .
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv run ruff check .
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache uv run ty check
```

Expected: all checks exit 0 without diagnostics.

- [ ] **Step 3: Run the complete quality gate**

```bash
MPLCONFIGDIR=/private/tmp/osm-polygon-description-tag-mplconfig \
UV_CACHE_DIR=/private/tmp/osm-polygon-description-tag-uv-cache \
just check
```

Expected: lock check, pre-commit, Ruff, ty, 1,347+ passing tests with coverage
above 90%, and `uv build` all pass. If only PyPI DNS blocks the build, retry
the unchanged `uv build` with network access and record that environmental fact.

### Task 5: Verify real-data equivalence and speed

**Files:**
- No additional files

- [ ] **Step 1: Build into a fresh temporary root**

Run the existing Afghanistan PBF build command with `/usr/bin/time -l`, a fresh
temporary data root, and the immutable Seagate source. Record counts, wall
time, output hash, and available resource metrics.

- [ ] **Step 2: Compare against the baseline artifact**

Compare Parquet SHA-256 and normalized manifest metadata against
`/private/tmp/osm-polygon-description-tag-perf-optimized.ndO10V` (105.67-second
baseline). Require identical output bytes, counts, source identity, schema,
algorithm revisions, dependencies, and rejection data. Timestamps and
`code_revision` may differ.

- [ ] **Step 3: Commit the verified implementation**

```bash
git add pyproject.toml uv.lock src/osm_polygon_description_tag/osm/extraction.py tests/unit/osm/test_extraction_helpers.py
git commit -m "perf: accelerate common COPY parsing path"
```

Expected: one focused commit with no generated data or unrelated changes.
