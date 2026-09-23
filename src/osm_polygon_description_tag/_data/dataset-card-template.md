---
pretty_name: OSM Polygon Description Tag
license: odbl
language:
- multilingual
tags:
- geospatial
- openstreetmap
- geoparquet
configs:
- config_name: default
  data_files:
  - split: train
    path: data/*.parquet
---

![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

# OSM Polygon Description Tag

OpenStreetMap polygons with a non-empty `description` or
`description:<suffix>` tag. Each row keeps the original tags, geometry,
WGS84 area, bounding box, and OSM provenance.

The `default` configuration is the polygon GeoParquet dataset. The optional
`language-v1` configuration has one row per description value with a detected
language and sentence splits when supported.

Source: [GitHub](https://github.com/NoeFlandre/osm-polygon-description-tag) ·
[Trackio](https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/?project=osm-polygon-description-tag&sidebar=hidden) ·
[dataset presentation](https://noeflandre.github.io/osm-polygon-description-tag/slides/dataset/dataset.html)

The map and generated counts use canonical globally unique
`(osm_type, osm_id)` polygons. Regional/raw rows and overlap duplicates are
reported separately.

<!-- GENERATED:H3_MAP:START -->
![H3 density of canonical globally unique `(osm_type, osm_id)` polygons with successfully extracted trimmed non-empty description text](assets/description_polygon_density.png)
<!-- GENERATED:H3_MAP:END -->
Hexbin density of canonical globally unique `(osm_type, osm_id)` polygons
with successfully extracted trimmed non-empty description text at H3 resolution
3, drawn from each canonical row's geometry centroid on a logarithmic scale.
Regional overlap duplicates are removed globally; this is not a count of
regional rows. Lighter cells contain more polygons.

<!-- GENERATED:STATS:START -->
<!-- GENERATED:STATS:END -->

## Terminology

- **Base description:** the exact `description=*` value.
- **Localized description:** the exact `description:<suffix>=*` value; the
  suffix is preserved and is not validated as a language code.
- **Canonical polygon:** one row per `(osm_type, osm_id)` after regional
  overlap duplicates are removed.

## Schema

- **Identity:** `source_pbf`, `osm_type`, `osm_id`, `osm_url`
- **OSM provenance:** `version`, `changeset`, `timestamp`
- **Text:** `name`, `localized_names`, `description`,
  `localized_descriptions`, `tags`
- **Spatial:** `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`,
  `bbox_max_x`, `bbox_max_y`, `geometry`

`geometry` is WKB with GeoParquet 1.1 metadata and OGC:CRS84 longitude/latitude
semantics. `tags` is the authoritative source for tag values.

## Load the data

Use any GeoParquet reader on `data/<region>-latest.parquet`. The language
configuration uses the same train split under `language-v1/data/`.

## Methodology

`osmium export` emits valid polygon features from OSM closed ways and
multipolygon or boundary relations. The pipeline keeps rows with at least one
trimmed, non-empty description value, computes geodesic WGS84 area, validates
GeoParquet and manifest identities, and writes deterministic artifacts.

Statistics and plots are generated from the published Parquet files. Global
counts use one canonical row per `(osm_type, osm_id)`; no sampling or external
lookup is used.

## Limitations

- OSM descriptions vary in quality, language, formatting, and completeness.
- Description suffixes are opaque and are not language labels.
- Geometry and tags reflect the source extracts at their recorded OSM times.

## License and attribution

Derived data is © OpenStreetMap contributors and available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/) (ODbL).
Follow its attribution and share-alike requirements. Pipeline code is
Apache-2.0.

## Reproducibility

The [source repository](https://github.com/NoeFlandre/osm-polygon-description-tag)
contains the extraction policy, validation code, and resumable workflows.

## Citation

Cite the repository using [`CITATION.cff`](https://github.com/NoeFlandre/osm-polygon-description-tag/blob/main/CITATION.cff).
