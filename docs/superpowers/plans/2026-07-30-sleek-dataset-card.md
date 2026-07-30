# Sleek Public Dataset Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a concise public dataset card with separate factual base and localized description word statistics while retaining complete per-file provenance in `stats.json`.

**Architecture:** Extend the existing bounded DuckDB reporting aggregation with deterministic description-value word statistics, then replace only the generated README renderer and packaged card template. Keep the detailed statistics payload, artifact validation, atomic write-if-changed behavior, metadata resumability, and publication boundaries unchanged.

**Tech Stack:** Python 3.12, uv, DuckDB, PyArrow, pytest, Ruff, ty.

---

## File map

- Modify `src/osm_polygon_description_tag/dataset/reporting.py`: collect bounded word statistics, bump the statistics schema, and render the compact generated dashboard.
- Modify `src/osm_polygon_description_tag/_data/dataset-card-template.md`: provide the canonical concise public card.
- Modify `docs/dataset-card-template.md`: keep the repository-visible template byte-identical to the packaged resource.
- Modify `tests/unit/dataset/test_reporting.py`: prove separate description-value and word aggregation.
- Modify `tests/contracts/test_dataset_card.py`: pin the concise public card and detailed `stats.json` boundary.
- Modify `tests/unit/dataset/test_deterministic_docs.py`: prove deterministic top-ten suffix presentation and metadata stability.

### Task 1: Add separate deterministic word statistics

**Files:**
- Modify: `tests/unit/dataset/test_reporting.py`
- Modify: `src/osm_polygon_description_tag/dataset/reporting.py`

- [ ] **Step 1: Write the failing aggregation tests**

Add synthetic records whose exact tags include:

```python
{
    "description": "Two words",
    "description:en": "three localized words",
    "description:fr": "quatre\u2003mots",
}
{
    "description": "One",
    "description:en": "single",
}
```

Assert:

```python
assert stats["stats_schema_version"] == 3
assert stats["base_description_values"] == 2
assert stats["base_description_words_total"] == 3
assert stats["base_description_words_median"] == 1.5
assert stats["localized_description_values"] == 3
assert stats["localized_description_words_total"] == 6
assert stats["localized_description_words_median"] == 2.0
```

Add an empty-category case asserting zero value/word totals and null medians.

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest tests/unit/dataset/test_reporting.py -q
```

Expected: FAIL because the six word-stat fields do not exist and the statistics schema is still version 2.

- [ ] **Step 3: Implement bounded DuckDB aggregation**

In `reporting.py`, bump `_STATS_SCHEMA_VERSION` to `3`. Add a private query helper that accepts only `base` or `localized` and uses DuckDB to count non-empty Unicode whitespace-delimited runs:

```sql
list_count(regexp_extract_all(value, '[^\s\p{Z}]+'))
```

For base values, query non-null `description` rows. For localized values, unnest every `map_entries(localized_descriptions)` value. Each helper returns:

```python
tuple[int, int, float | None]
```

representing value count, total words, and `quantile_cont(word_count, 0.5)`.
Return zeros and `None` for an empty category. Add the six fields to the
canonical `stats.json` payload without removing existing fields.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
uv run pytest tests/unit/dataset/test_reporting.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/osm_polygon_description_tag/dataset/reporting.py \
  tests/unit/dataset/test_reporting.py
git commit -m "feat: add description word statistics"
```

### Task 2: Render a sleek public card

**Files:**
- Modify: `tests/contracts/test_dataset_card.py`
- Modify: `tests/unit/dataset/test_deterministic_docs.py`
- Modify: `src/osm_polygon_description_tag/dataset/reporting.py`
- Modify: `src/osm_polygon_description_tag/_data/dataset-card-template.md`
- Modify: `docs/dataset-card-template.md`

- [ ] **Step 1: Write failing public-card contracts**

Assert the generated README contains:

```python
assert "## Dataset at a glance" in readme
assert "## Description coverage" in readme
assert "Base descriptions" in readme
assert "Localized descriptions" in readme
assert "Total words" in readme
assert "Median words per description" in readme
assert "Detailed machine-readable statistics" in readme
```

Assert it does not contain:

```python
assert "Files (deterministic, sorted by parquet filename)" not in readme
assert "Source SHA-256" not in readme
assert "Transformation rejections by reason" not in readme
```

Parse `stats.json` and prove `files`, `source_sha256`, `output_sha256`, and
`rejections` remain present.

Create at least eleven suffixes and assert only the deterministic top ten are
rendered, ordered by descending count and exact suffix ascending for ties.

- [ ] **Step 2: Run the focused contracts to verify RED**

Run:

```bash
uv run pytest tests/contracts/test_dataset_card.py \
  tests/unit/dataset/test_deterministic_docs.py -q
```

Expected: FAIL because the old generated block renders exhaustive tables and the template remains verbose.

- [ ] **Step 3: Replace the generated renderer**

Change `_render_stats_block` to render:

- hidden stats/schema identity comments;
- `## Dataset at a glance` with polygons, Parquet files, output size, ways,
  relations, Polygons, and MultiPolygons;
- `## Description coverage` with separate value count, total words, and median
  word count for base and localized descriptions;
- a top-ten localized suffix table sorted by `(-count, suffix)`;
- a compact area summary and OSM timestamp range when present;
- a link to `stats.json` for complete machine-readable facts.

Do not render full suffix, rejection, or per-file tables.

- [ ] **Step 4: Replace and synchronize the templates**

Write one concise card with:

- one-paragraph scope;
- the generated block;
- grouped schema bullets covering identifiers/provenance, description/name
  views, complete tags, geometry, area, and bounding boxes;
- one PyArrow and one GeoPandas example;
- short methodology, limitations, attribution/license, and reproducibility
  sections.

Copy the exact bytes to both template paths and retain the generated markers
and Hugging Face YAML front matter.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```bash
uv run pytest tests/contracts/test_dataset_card.py \
  tests/unit/dataset/test_reporting.py \
  tests/unit/dataset/test_deterministic_docs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/osm_polygon_description_tag/dataset/reporting.py \
  src/osm_polygon_description_tag/_data/dataset-card-template.md \
  docs/dataset-card-template.md \
  tests/contracts/test_dataset_card.py \
  tests/unit/dataset/test_deterministic_docs.py
git commit -m "feat: generate sleek public dataset card"
```

### Task 3: Verify, publish code, and establish the resume boundary

**Files:**
- Modify only if a verification defect is found.

- [ ] **Step 1: Verify template identity and stale terminology**

Run:

```bash
cmp src/osm_polygon_description_tag/_data/dataset-card-template.md \
  docs/dataset-card-template.md
rg -n "Files \\(deterministic|Source SHA-256|Transformation rejections" \
  src/osm_polygon_description_tag/_data/dataset-card-template.md \
  docs/dataset-card-template.md
```

Expected: templates are byte-identical and the stale exhaustive-card phrases are absent.

- [ ] **Step 2: Run the authoritative local gate**

Run:

```bash
just check
```

Expected: lock, pre-commit, Ruff format/lint, ty, all pytest tests with at
least 90% coverage and zero skips, and package build all pass.

- [ ] **Step 3: Verify wheel contents and CLI**

Run:

```bash
uv run osm-polygon-description-tag --help
uv run osm-polygon-description-tag run-and-publish --help
```

Inspect the built wheel and assert it contains the changed packaged template.

- [ ] **Step 4: Verify operational isolation**

Read the active process and latest JSONL events without signaling it. Confirm
the feature worktree is clean and the active local `main` checkout remains at
its pre-run `HEAD`. Do not run `generate-card` against the Seagate data root.

- [ ] **Step 5: Push without changing the running checkout**

Push the verified feature history as a fast-forward directly to remote
`main`. Confirm the remote `main` SHA. Leave the local `main` checkout and
running process untouched.

- [ ] **Step 6: Report the safe resume procedure**

If the old process is still active, instruct the operator not to start a
second instance. After it exits:

```bash
git pull --ff-only
just run-and-publish
```

The first command loads the new generator; the single pipeline command reuses
existing validated data and republishes deterministic metadata as needed.
