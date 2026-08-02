---
title: "OSM Polygon Description Tag · Dataset"
subtitle: "A research snapshot of described polygons in OpenStreetMap"
author: "Noé Flandre"
date: "2026-08"
aspect_ratio: "16:9"
theme: default
fonts:
  heading: "Inter"
  body: "Inter"
figure_captions: false
footer:
  left: "OSM Polygon Description Tag · dataset"
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
  .balanced { padding-top: 110px; }
  .slide p, .slide li, .slide td, .slide th { font-size: 1.06rem; line-height: 1.42; }
  .kicker { color: #17624f; font-size: 0.82rem; letter-spacing: 0.12em; text-transform: uppercase; font-weight: 700; }
  .source-note { color: #68716c; font-size: 0.66rem; line-height: 1.2; position: absolute; bottom: 2.7rem; left: 5.6rem; right: 5.6rem; }
  .big-number { color: #17624f; font-size: 3.1rem; line-height: 1; font-weight: 750; }
  .metric-label { color: #5e6862; font-size: 0.82rem; line-height: 1.2; text-transform: uppercase; letter-spacing: 0.07em; }
  .rule { border-top: 2px solid #17624f; width: 5rem; margin: 1.2rem 0; }
  .accent { color: #17624f; }
  .muted { color: #5e6862; }
  .dark-note { background: #e9eeea; border-left: 4px solid #17624f; padding: 0.75rem 1rem; }
  .slide img { max-height: 27rem; object-fit: contain; }
  .plot-slide .slide-content > p:first-child,
  .plot-slide .slide-content > p:nth-child(2) {
    width: fit-content;
    margin-left: auto;
    margin-right: auto;
  }
---

<!-- padding: roomy -->

<div class="kicker">Dataset briefing · snapshot 2026-08</div>

# A global atlas of described OSM polygons

906,631 deduplicated polygon rows, each carrying the source tags, geometry,
area, provenance, and exact description text that made it discoverable.

Open dataset · GeoParquet 1.1 · ODbL-derived OpenStreetMap data

<div class="source-note">Sources: generated stats.json; docs/dataset-contract.md; dataset card snapshot</div>

---

<!-- padding: compact -->
<!-- class: balanced -->

## The inclusion rule is simple and deliberately narrow

Keep a feature when it is a polygon and at least one exact description tag is
non-empty.

<!-- columns: 2 -->

**Included**

- Tagged closed ways that OSM classifies as areas
- `type=multipolygon` and `type=boundary` relations that assemble
- `description=*` or `description:<suffix>=*`
- `area=yes` remains authoritative

|||

**Excluded**

- Nodes and open ways
- `area=no`
- Empty or whitespace-only descriptions
- Failed relation assemblies and non-polygon output

<div class="source-note">Sources: docs/dataset-contract.md; config/osmium-export.json; generated stats.json</div>

---

<!-- padding: compact -->
<!-- class: balanced -->

## The snapshot is large in input, selective in output

<!-- columns: 3 -->

<div class="big-number">83.0 GB</div>
<div class="metric-label">source PBF bytes</div>

|||

<div class="big-number">869.9M</div>
<div class="metric-label">emitted OSM features</div>

|||

<div class="big-number">906.6k</div>
<div class="metric-label">published rows</div>

386 regional PBF extracts become 386 Parquet files totaling 712.4 MiB. The
final rows are unique by `(osm_type, osm_id)`; 38,851 duplicate candidates were
removed during global deduplication.

<div class="source-note">Source: generated stats.json; values rounded for presentation, exact values remain in stats.json</div>

---

<!-- layout: section-break -->

## Read each row as a small, queryable OSM record

The dataset keeps both the source authority and analysis-ready projections.

---

<!-- padding: compact -->
<!-- class: balanced -->

## Every row keeps the evidence behind the label

<!-- columns: 2 -->

**Text and provenance**

- Base and localized names
- Base and localized descriptions
- Complete original `tags` map
- OSM version, changeset, timestamp, source PBF, and URL

|||

**Geometry and measurement**

- Full Polygon or MultiPolygon WKB
- OGC:CRS84 longitude/latitude semantics
- WGS84 geodesic `area_m2`
- Bounding box covering the complete geometry

<div class="dark-note">The convenience columns are exact derived views;
`tags` is the authoritative source map.</div>

<div class="source-note">Sources: docs/dataset-contract.md; generated stats.json</div>

---

<!-- padding: compact -->
<!-- class: balanced -->

## Descriptions are predominantly base text, with a multilingual tail

<!-- columns: 2 -->

**Base descriptions**

<div class="big-number">887,077</div>
<div class="metric-label">values · 5.11M words · median 4 words</div>

|||

**Localized descriptions**

<div class="big-number">32,049</div>
<div class="metric-label">values · 214k words · median 3 words</div>

The most common exact localized suffixes are `de`, `en`, `it`, `fr`, and `ru`.
Suffixes are preserved verbatim; they are not asserted to be valid language
codes.

<div class="source-note">Source: generated stats.json; suffix interpretation from docs/dataset-contract.md</div>

---

<!-- padding: compact -->
<!-- class: plot-slide -->

## The global distribution is dense but uneven

![H3 density of description-tagged polygons](assets/description_polygon_density.png)

7,381 H3 resolution-3 cells contain rows; log scale keeps sparse and dense regions visible.

<div class="source-note">Source: assets/description_polygon_density.png; docs/dataset-contract.md; generated stats.json</div>

---

<!-- padding: compact -->
<!-- class: plot-slide -->

## Most polygons are small, but the tail reaches country scale

![Area distribution of description-tagged polygons](assets/area_distribution.png)

Median area: **501 m²** · middle 50%: **85–10,391 m²** · range:
**0.000062–3.48×10¹² m²**.

<div class="source-note">Source: assets/area_distribution.png; generated stats.json; area_m2 is WGS84 geodesic area</div>

---

<!-- padding: compact -->
<!-- class: balanced -->

## The timestamps span the history of modern OSM mapping

<!-- columns: 2 -->

<div class="big-number">2007 → 2026</div>
<div class="metric-label">minimum to maximum OSM object timestamp (UTC)</div>

|||

The timestamp is object provenance, not a dataset publication date. Text and
geometry reflect the source extracts at their recorded OSM timestamps.

<div class="source-note">Source: generated stats.json; docs/dataset-contract.md</div>

---

<!-- padding: compact -->
<!-- class: balanced -->

## Interpretation needs three guardrails

**Language** — localized suffixes are exact OSM keys, not validated language
codes.

**Text quality** — descriptions are community-authored and vary in language,
formatting, completeness, and quality.

**Spatial meaning** — the H3 map counts polygon centroids and the extracts are
regional snapshots; the dataset is not a population or land-cover estimate.

<div class="source-note">Sources: dataset-card-template.md; docs/dataset-contract.md</div>

---

<!-- padding: roomy -->

<div class="kicker">Access and reuse</div>

# Explore the rows, then reproduce the snapshot

Dataset: [huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag](https://huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag)

Metrics: [Trackio snapshot dashboard](https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/?project=osm-polygon-description-tag&sidebar=hidden)

Source and methodology: [github.com/NoeFlandre/osm-polygon-description-tag](https://github.com/NoeFlandre/osm-polygon-description-tag)

Derived data is © OpenStreetMap contributors and available under ODbL.

<div class="source-note">Sources: generated README.md; docs/index.md; docs/dataset-contract.md</div>
