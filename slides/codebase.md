---
title: "OSM Polygon Description Tag · Codebase"
subtitle: "A deterministic, resumable data product pipeline"
author: "Noé Flandre"
date: "2026-08"
aspect_ratio: "16:9"
theme: default
fonts:
  heading: "Inter"
  body: "Inter"
figure_captions: false
footer:
  left: "OSM Polygon Description Tag · codebase"
  right: "auto"
custom_css: |
  :root {
    --colloquium-bg: #f6f5f0;
    --colloquium-text: #20221f;
    --colloquium-heading: #10231d;
    --colloquium-accent: #17624f;
    --colloquium-link: #17624f;
    --colloquium-progress-fill: #17624f;
    --colloquium-code-bg: #e9eeea;
    --colloquium-muted: #5e6862;
    --colloquium-border: #d7ddd7;
    --colloquium-progress-bg: #dde4dd;
  }
  .slide { background: #f6f5f0; }
  .slide--section-break { background: #102f27; }
  .slide--section-break h2, .slide--section-break p { color: #f6f5f0; }
  .slide h1, .slide h2 { letter-spacing: -0.025em; }
  .slide h1 { font-size: 3.25rem; line-height: 1.02; }
  .slide h2 { font-size: 2.25rem; line-height: 1.06; }
  .slide p, .slide li, .slide td, .slide th { font-size: 1.06rem; line-height: 1.42; }
  .slide code { color: #124c3e; }
  .slide pre code { font-size: 0.78em; line-height: 1.42; }
  .kicker { color: #17624f; font-size: 0.82rem; letter-spacing: 0.12em; text-transform: uppercase; font-weight: 700; }
  .source-note { color: #68716c; font-size: 0.66rem; line-height: 1.2; position: absolute; bottom: 2.7rem; left: 5.6rem; right: 5.6rem; }
  .big-number { color: #17624f; font-size: 3.1rem; line-height: 1; font-weight: 750; }
  .metric-label { color: #5e6862; font-size: 0.82rem; line-height: 1.2; text-transform: uppercase; letter-spacing: 0.07em; }
  .rule { border-top: 2px solid #17624f; width: 5rem; margin: 1.2rem 0; }
  .accent { color: #17624f; }
  .muted { color: #5e6862; }
  .dark-note { background: #e9eeea; border-left: 4px solid #17624f; padding: 0.75rem 1rem; }
---

<!-- padding: roomy -->

<div class="kicker">Codebase briefing · 2026</div>

# A pipeline built to survive interruption

## OSM Polygon Description Tag

One validated GeoParquet artifact per source PBF — with explicit contracts at
every boundary.

<div class="rule"></div>

`uv` · `Ruff` · `ty` · `pytest` · `Typer` · `Just` · GitHub Actions

<div class="source-note">Sources: README.md; docs/architecture.md; docs/operations.md</div>

---

<!-- padding: compact -->

## The codebase has one job: make the dataset repeatable

The system turns a read-only corpus of OSM extracts into a public artifact that
can be audited, interrupted, resumed, and reproduced.

<!-- columns: 2 -->

**The contract**

- One output Parquet per discovered `*.osm.pbf`
- Full geometry, tags, provenance, and geodesic area
- Statistics and visual assets derived from finalized files
- Explicit Hugging Face plans, verification, and state

|||

**The boundaries**

```text
code checkout
  /Users/noeflandre/osm-polygon-description-tag

immutable raw PBFs
  /Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw

generated data + state
  /Volumes/Seagate M3/projects/osm-polygon-description-tag
```

<div class="source-note">Sources: docs/index.md; docs/operations.md; docs/getting-started.md</div>

---

<!-- layout: section-break -->

## The architecture is a one-way flow

Lower-level packages do not import higher-level orchestration. Compatibility
modules re-export canonical APIs; they do not duplicate business logic.

```text
runtime  →  osm  →  dataset  →  publication
   │         │        │             │
   └─────────┴────────┴─────────────┴──→ workflow  →  cli

observability (optional Trackio metrics)
```

<div class="source-note">Source: docs/architecture.md</div>

---

<!-- padding: compact -->

## Ingest is deterministic before it is fast

Every source follows the same bounded path.

<!-- columns: 2 -->

**1 · Discover**

Sort direct PBF files by filename. Capture size, mtime, and source SHA-256.
Reject symlinks and unsafe paths.

**2 · Export**

`osmium export` applies standard OSM area handling and emits polygon geometry
as a streamed PostgreSQL `COPY` record.

|||

**3 · Transform**

Keep `way` and `relation` records with a non-empty `description` or
`description:<suffix>`. Decode valid Polygon/MultiPolygon WKB and calculate
WGS84 geodesic area.

```text
PBF → osmium → COPY stream → transform_record → row contract
```

<div class="source-note">Sources: docs/dataset-contract.md; src/osm_polygon_description_tag/osm; src/osm_polygon_description_tag/dataset/transform.py</div>

---

<!-- padding: compact -->

## The row contract keeps convenience and authority separate

`tags` remains the complete original OSM tag map. Projected columns make common
queries easier without discarding source detail.

<!-- columns: 2 -->

| Group | Fields |
|---|---|
| Identity | `source_pbf`, `osm_type`, `osm_id`, `osm_url` |
| OSM provenance | `version`, `changeset`, `timestamp` |
| Text views | `name`, `localized_names`, `description`, `localized_descriptions` |
| Spatial | `geometry_type`, `area_m2`, four bbox fields, `geometry` |

|||

<div class="dark-note">

**Versioned format**

Arrow schema version **2** · GeoParquet **1.1.0** · WGS84 / OGC:CRS84 ·
Polygon or MultiPolygon WKB · strictly positive geodesic `area_m2`.

</div>

<div class="source-note">Sources: docs/dataset-contract.md; src/osm_polygon_description_tag/dataset/schema.py</div>

---

<!-- padding: compact -->

## Atomic artifacts make partial work harmless

The writer never promotes an unvalidated file.

```text
records
  ↓ bounded batches
owned temporary Parquet
  ↓ final GeoParquet metadata + validation
fsync file → fsync directory → os.replace
  ↓
final Parquet + matching manifest
```

Manifests bind source identity, output identity, schema version, transform
version, area policy, tool versions, and factual counts.

<div class="dark-note">A crash can leave owned temporary work, but not a
half-promoted finalized artifact.</div>

<div class="source-note">Sources: docs/dataset-contract.md; src/osm_polygon_description_tag/dataset/storage.py; src/osm_polygon_description_tag/dataset/manifest.py</div>

---

<!-- padding: compact -->

## Deduplication is global, deterministic, and resumable

Regional extracts can contain the same OSM object. The deduplication stage
keeps exactly one row per `(osm_type, osm_id)`.

```text
highest OSM version
        ↓ tie
latest timestamp
        ↓ tie
lexicographically smallest source_pbf
        ↓ tie
stable row fingerprint
```

Only affected Parquet files and manifests are rewritten. The staged state under
`.work/dedup-state.json` lets the next invocation finish safely after a stop.

<div class="source-note">Sources: docs/dataset-contract.md; src/osm_polygon_description_tag/dataset/deduplication.py</div>

---

<!-- padding: compact -->

## Reporting is a derived view, not a second source of truth

The card, `stats.json`, H3 map, area histogram, and Trackio snapshot are built
from validated final Parquets and matching manifests.

<!-- columns: 2 -->

**Deterministic outputs**

- fixed schema and transform versions
- sorted file identities and exact SHA-256 values
- atomic write-if-changed metadata
- map and histogram cache identities

|||

**Operational consequence**

Changing the README template does not require recomputing the plots. Changing
the finalized dataset or renderer version does.

<div class="source-note">Sources: src/osm_polygon_description_tag/dataset/reporting.py; src/osm_polygon_description_tag/dataset/geography</div>

---

<!-- padding: compact -->

## Publication is an allowlisted, verified transition

The Hub is treated as an external system, not as a writable folder.

```text
preflight
  → exact plan identity
  → upload only named files
  → verify remote SHA-256 / size
  → atomically record publication state
```

Per-source plans contain the Parquet, manifest, README, stats, H3 map, and area
histogram. Logs, caches, temporary files, `.DS_Store`, and publication state
never enter a plan.

<div class="source-note">Sources: docs/operations.md; src/osm_polygon_description_tag/publication/planning.py; src/osm_polygon_description_tag/publication/verification.py</div>

---

<!-- padding: compact -->

## One command is enough — and Ctrl-C is part of the design

```bash
just run-and-publish
```

The orchestrator classifies each source as:

`build`  ·  `reuse-local`  ·  `already-published`

Press **Ctrl-C once**, wait for exit code **130**, then rerun the same command.
Finalized Parquets, manifests, and verified state remain available for reuse.

<div class="dark-note">Do not start a second instance against the same data
root.</div>

<div class="source-note">Sources: docs/operations.md; docs/cli.md; src/osm_polygon_description_tag/workflow/orchestrator.py</div>

---

<!-- padding: compact -->

## The quality bar is executable

The repository keeps behavior close to the contract.

<!-- columns: 2 -->

**Developer loop**

```text
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

|||

**Current verification snapshot**

`612` tests passed in the final audit. The same toolchain is exposed through
pre-commit, Just, and GitHub Actions.

<div class="source-note">Sources: pyproject.toml; docs/development.md; tests/</div>

---

<!-- padding: roomy -->

<div class="kicker">Operator handoff</div>

# Read the contract, then run the workflow

```bash
cd /Users/noeflandre/osm-polygon-description-tag
uv sync --locked
just run-and-publish
```

The codebase is intentionally conservative: validate first, write atomically,
publish explicitly, and make every resume decision inspectable.

<div class="source-note">Sources: docs/getting-started.md; docs/operations.md; README.md</div>
