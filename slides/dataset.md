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
    --colloquium-code-bg: #e9eeea;
    --colloquium-muted: #5e6862;
    --colloquium-border: #d7ddd7;
    --colloquium-progress-bg: #dde4dd;
    --colloquium-progress-fill: #17624f;
    --slide-pad-x: 86px;
    --slide-pad-y: 70px;
    --slide-safe-bottom: 62px;
    --content-max: 1080px;
  }

  .slide {
    overflow: hidden;
    background: var(--colloquium-bg);
    color: var(--colloquium-text);
    padding: var(--slide-pad-y) var(--slide-pad-x) var(--slide-safe-bottom);
  }

  .pad-compact.active,
  .pad-roomy.active {
    padding: var(--slide-pad-y) var(--slide-pad-x) var(--slide-safe-bottom);
  }

  .slide h1,
  .slide h2 {
    max-width: var(--content-max);
    margin-left: auto;
    margin-right: auto;
    color: var(--colloquium-heading);
    font-weight: 780;
    letter-spacing: -0.045em;
    text-wrap: balance;
  }

  .slide h1 {
    font-size: 3.2rem;
    line-height: 0.98;
    margin-bottom: 0.85rem;
  }

  .slide h2 {
    width: min(100%, var(--content-max));
    font-size: 2.18rem;
    line-height: 1.06;
    margin-bottom: 1.35rem;
  }

  .slide p,
  .slide li,
  .slide td,
  .slide th {
    font-size: 1.02rem;
    line-height: 1.42;
  }

  .slide p { margin-bottom: 0.75rem; }
  .slide ul { margin-top: 0.5rem; padding-left: 1.1rem; }
  .slide li { margin-bottom: 0.34rem; }
  .slide li::marker { color: var(--colloquium-accent); }

  .slide code {
    background: var(--colloquium-code-bg);
    border-radius: 0.28rem;
    padding: 0.1rem 0.35rem;
    color: var(--colloquium-text);
  }

  .slide--content,
  .balanced,
  .metric-slide,
  .plot-slide {
    justify-content: center;
  }

  .slide--content > .slide-content,
  .slide--section-break > .slide-content {
    flex: 0 0 auto;
    width: min(100%, var(--content-max));
    margin-left: auto;
    margin-right: auto;
  }

  .kicker {
    color: var(--colloquium-accent);
    font-size: 0.76rem;
    line-height: 1.2;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    font-weight: 760;
  }

  .source-note {
    position: absolute;
    left: var(--slide-pad-x);
    right: var(--slide-pad-x);
    bottom: 3.05rem;
    color: #68716c;
    font-size: 0.58rem;
    line-height: 1.25;
    letter-spacing: 0.01em;
  }

  .big-number {
    color: var(--colloquium-accent);
    font-size: 3.25rem;
    line-height: 0.95;
    font-weight: 780;
    letter-spacing: -0.04em;
    font-variant-numeric: tabular-nums;
  }

  .metric-label {
    margin-top: 0.42rem;
    color: var(--colloquium-muted);
    font-size: 0.76rem;
    line-height: 1.24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 560;
  }

  .accent { color: var(--colloquium-accent); }
  .muted { color: var(--colloquium-muted); }

  .slide--title {
    justify-content: center;
    align-items: center;
    text-align: center;
    padding: 76px 92px 68px;
  }

  .slide--title h1 {
    width: min(100%, 1110px);
    font-size: 3.28rem;
    line-height: 0.98;
    margin-bottom: 0.9rem;
  }

  .slide--title .slide-content {
    flex: 0 0 auto;
    width: min(100%, 1040px);
    margin-left: auto;
    margin-right: auto;
  }

  .slide--title .slide-content p {
    max-width: 980px;
    margin: 0.72rem auto 0;
    font-size: 1.22rem;
    line-height: 1.38;
    color: var(--colloquium-muted);
  }

  .slide--title .kicker {
    margin: 0.05rem 0 0.2rem;
  }

  .slide--section-break {
    justify-content: center;
    align-items: center;
    text-align: center;
    background: #102f27;
  }

  .slide--section-break h2 {
    width: min(100%, 920px);
    color: #f6f5f0;
    font-size: 2.35rem;
    line-height: 1.08;
    margin-bottom: 1rem;
  }

  .slide--section-break p {
    color: rgba(246, 245, 240, 0.9);
    font-size: 1.16rem;
  }

  .slide--section-break .source-note {
    color: rgba(246, 245, 240, 0.62);
  }

  .slide--content.cols-2 .colloquium-grid,
  .slide--content.cols-3 .colloquium-grid {
    align-items: start;
    gap: 3.1rem;
  }

  .slide--content.cols-2 .colloquium-grid > .col + .col,
  .slide--content.cols-3 .colloquium-grid > .col + .col {
    border-left: 1px solid var(--colloquium-border);
    padding-left: 3.1rem;
  }

  .slide--content .colloquium-grid > .col > p:first-child strong {
    font-size: 1.04rem;
  }

  .inclusion-slide .colloquium-grid > .col + .col {
    padding-top: 4.2rem;
  }

  .dark-note {
    margin-top: 1rem;
    margin-bottom: 0;
    background: #eef2ee;
    border-left: 4px solid var(--colloquium-accent);
    padding: 0.9rem 1.05rem;
    color: var(--colloquium-text);
  }

  .dark-note,
  .dark-note p {
    font-size: 1.06rem;
    line-height: 1.4;
  }

  .snapshot-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 3.1rem;
    align-items: start;
    width: 100%;
    margin-top: 0.15rem;
  }

  .snapshot-metric + .snapshot-metric {
    border-left: 1px solid var(--colloquium-border);
    padding-left: 3.1rem;
  }

  .snapshot-summary {
    grid-column: 1 / -1;
    max-width: 930px;
    margin: 1.9rem auto 0;
    padding-top: 1.05rem;
    border-top: 2px solid var(--colloquium-accent);
    color: var(--colloquium-muted);
    text-align: center;
  }

  .metric-slide > .slide-content {
    width: min(100%, 1060px);
  }

  .metric-slide .metric-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 3.6rem;
    align-items: start;
    width: 100%;
  }

  .metric-slide .metric-block {
    min-height: 7.2rem;
  }

  .metric-slide .metric-block + .metric-block {
    border-left: 1px solid var(--colloquium-border);
    padding-left: 3.6rem;
  }

  .metric-slide .metric-heading {
    margin-bottom: 0.62rem;
    font-size: 1.04rem;
    line-height: 1.36;
    font-weight: 720;
  }

  .metric-slide .metric-summary {
    grid-column: 1 / -1;
    max-width: 820px;
    margin: 1.7rem auto 0;
    padding-top: 1.05rem;
    border-top: 2px solid var(--colloquium-accent);
    color: var(--colloquium-muted);
    text-align: center;
  }

  .plot-slide > .slide-content {
    width: min(100%, 1080px);
    text-align: center;
  }

  .plot-slide .slide-content > p:first-child {
    width: 100%;
    margin: 0 auto 0.7rem;
  }

  .plot-slide .slide-content > p:nth-child(2) {
    max-width: 920px;
    margin: 0.25rem auto 0;
    text-align: center;
  }

  .plot-slide img {
    display: block;
    width: auto;
    max-width: 100%;
    max-height: 390px;
    margin-left: auto;
    margin-right: auto;
    object-fit: contain;
    border-radius: 0.35rem;
  }

  .guardrail-slide > .slide-content {
    width: min(100%, 900px);
  }

  .guardrail-slide .slide-content > p {
    margin-bottom: 1.05rem;
  }

  .timestamp-slide .colloquium-grid {
    align-items: center;
  }

  .timestamp-slide .colloquium-grid > .col {
    min-height: 7.8rem;
  }

  .timestamp-slide .colloquium-grid > .col:first-child {
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .timestamp-slide .colloquium-grid > .col + .col {
    display: flex;
    align-items: center;
  }

  .timestamp-slide .big-number {
    font-size: 3.45rem;
  }

  .slide pre { margin: 1rem auto; }

  @media print {
    .pad-compact,
    .pad-roomy {
      padding: var(--slide-pad-y) var(--slide-pad-x) var(--slide-safe-bottom) !important;
    }

    .slide--content,
    .balanced,
    .metric-slide,
    .plot-slide,
    .slide--section-break {
      justify-content: center !important;
    }

    .slide--content > .slide-content,
    .slide--section-break > .slide-content {
      flex: 0 0 auto !important;
      width: min(100%, var(--content-max)) !important;
      margin-left: auto !important;
      margin-right: auto !important;
    }
  }

---

<!-- padding: roomy -->

<div class="kicker">Dataset briefing · snapshot 2026-08</div>

# Worldwide OSM polygons with textual descriptions from the "description" tags

906,631 deduplicated polygon rows, each carrying the source tags, geometry,
area, provenance, and exact description text.

Open dataset · GeoParquet 1.1 · ODbL-derived OpenStreetMap data

<div class="source-note">Sources: generated stats.json; docs/dataset-contract.md; dataset card snapshot</div>

---

<!-- padding: compact -->
<!-- class: balanced inclusion-slide -->

## Which polygons do we keep?

Keep a polygon if it has at least one non empty description tag.

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
<!-- class: balanced snapshot-slide -->

## The snapshot is large in input, selective in output

<div class="snapshot-metrics">
  <div class="snapshot-metric">
    <div class="big-number">83.0 GB</div>
    <div class="metric-label">source PBF bytes</div>
  </div>
  <div class="snapshot-metric">
    <div class="big-number">869.9M</div>
    <div class="metric-label">emitted OSM features</div>
  </div>
  <div class="snapshot-metric">
    <div class="big-number">906.6k</div>
    <div class="metric-label">published rows</div>
  </div>
  <p class="snapshot-summary">386 regional PBF extracts become 386 Parquet files totaling 712.4 MiB. The final rows are unique by <code>(osm_type, osm_id)</code>; 38,851 duplicate candidates were removed during global deduplication.</p>
</div>

<div class="source-note">Source: generated stats.json; values rounded for presentation, exact values remain in stats.json</div>

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

<div class="source-note">Sources: docs/dataset-contract.md; generated stats.json</div>

---

<!-- padding: compact -->
<!-- class: metric-slide -->

## Descriptions are predominantly base text, with a multilingual tail

<div class="metric-grid">
  <div class="metric-block">
    <div class="metric-heading">Base descriptions</div>
    <div class="big-number">887,077</div>
    <div class="metric-label">values · 5.11M words · median 4 words</div>
  </div>
  <div class="metric-block">
    <div class="metric-heading">Localized descriptions</div>
    <div class="big-number">32,049</div>
    <div class="metric-label">values · 214k words · median 3 words</div>
  </div>
  <p class="metric-summary">The most common exact localized suffixes are <code>de</code>, <code>en</code>, <code>it</code>, <code>fr</code>, and <code>ru</code>. Suffixes are preserved verbatim; they are not asserted to be valid language codes.</p>
</div>

<div class="source-note">Source: generated stats.json; suffix interpretation from docs/dataset-contract.md</div>

---

<!-- padding: compact -->
<!-- class: plot-slide -->

## The global distribution is dense but uneven

![H3 density of description-tagged polygons](assets/description_polygon_density.png)

7,381 H3 resolution-3 cells contain rows.

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
<!-- class: balanced timestamp-slide -->

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
<!-- class: balanced guardrail-slide -->

## Interpretation needs three guardrails

**Language** — localized suffixes are exact OSM keys, not validated language
codes.

**Text quality** — descriptions are community-authored and vary in language,
formatting, completeness, and quality.

**Spatial meaning** — the H3 map counts polygon centroids and the extracts are
regional snapshots.

<div class="source-note">Sources: dataset-card-template.md; docs/dataset-contract.md</div>

---

<!-- padding: roomy -->

<div class="kicker">Access and reuse</div>

# Resources

Dataset: [huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag](https://huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag)

Metrics: [Trackio snapshot dashboard](https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/?project=osm-polygon-description-tag&sidebar=hidden)

Github: [github.com/NoeFlandre/osm-polygon-description-tag](https://github.com/NoeFlandre/osm-polygon-description-tag)

Derived data is © OpenStreetMap contributors and available under ODbL.

<div class="source-note">Sources: generated README.md; docs/index.md; docs/dataset-contract.md</div>
