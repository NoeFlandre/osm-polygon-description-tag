# Dataset contract

## Inclusion rule

The dataset retains polygonal OSM features with at least one exact, non-empty
description value:

- `description=*` for a base description;
- `description:<suffix>=*` for a localized description.

Osmium applies the standard area policy with `area_tags: true`, `linear_tags:
true`, and `--geometry-types polygon`. Closed ways with `area=no`, nodes, open
ways, and non-polygon outputs are excluded. `area=yes` remains authoritative.
`type=multipolygon` and `type=boundary` relations are included when assembly
produces a valid Polygon or MultiPolygon.

## Row schema

Every Parquet file uses the versioned GeoParquet schema (`SCHEMA_VERSION = 2`):

| Group | Columns |
| --- | --- |
| Identity | `source_pbf`, `osm_type`, `osm_id`, `osm_url` |
| OSM provenance | `version`, `changeset`, `timestamp` |
| Names | `name`, `localized_names` |
| Descriptions | `description`, `localized_descriptions` |
| Source authority | `tags` |
| Spatial | `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`, `bbox_max_x`, `bbox_max_y`, `geometry` |

`tags` is the complete original OSM tag map and remains authoritative. The
name and description columns are exact derived views for convenient querying.
Localized suffixes are preserved exactly and are not asserted to be valid
language codes.

## Geometry and area

`geometry` contains complete two-dimensional WKB with GeoParquet 1.1 metadata
and OGC:CRS84 longitude/latitude semantics. `geometry_type` is `Polygon` or
`MultiPolygon`.

`area_m2` is a positive WGS84 geodesic area. Ring orientation is normalized;
holes are subtracted and all MultiPolygon components are included. Bounding-box
columns cover the complete geometry in source coordinate order.

## Descriptions and words

The generated dataset card reports base and localized descriptions separately:

- number of description values;
- total whitespace-delimited words;
- median words per description value;
- the most common exact localized suffixes.

These values are calculated from validated Parquet rows. Full suffix counts,
per-file identities, rejection counts, and all other machine-readable facts
remain in the published `stats.json`.

## Reproducibility

Each source has one output Parquet and one manifest containing source/output
identities, schema and transform versions, tool versions, and factual counts.
Artifacts are written and promoted atomically. Statistics and the dataset card
are regenerated only from validated artifacts and matching manifests, with
byte-stable write-if-changed behavior.

## Global identity deduplication

Before publication, the workflow keeps exactly one row for each
`(osm_type, osm_id)` across all regional extracts. The canonical row is chosen
by highest OSM `version`, then latest `timestamp`, then lexicographically
smallest `source_pbf`, with a stable row fingerprint as the final tie-breaker.
Only affected per-PBF Parquets and manifests are rewritten. The operation is
atomic and resumable through local `.work/dedup-state.json`; raw PBFs are never
modified. Deduplicated rows are recorded as the factual
`duplicate_osm_object` rejection reason in manifests and `stats.json`.

## Map asset

In addition to the Parquet files, the dataset repository contains a single
map asset at `assets/description_polygon_density.png`. The map is a derived
publication artifact generated from the complete validated local dataset. It
is not part of the Parquet schema.

The map:

* uses H3 resolution 3;
* assigns each polygon to a cell by its Shapely geometry centroid;
* uses a logarithmic colour scale so sparse and dense areas remain visible;
* counts every deduplicated dataset row exactly once;
* is uploaded as part of every per-PBF plan and as part of the final
  metadata plan;
* is included in the publication allowlist under the exact filename
  `assets/description_polygon_density.png`; hidden, temporary, symlinked,
  and unrelated files under `assets/` are rejected;
* is recorded in `publication-state.json` with its SHA-256 and size so a
  change to the map bytes invalidates the metadata no-op path.
