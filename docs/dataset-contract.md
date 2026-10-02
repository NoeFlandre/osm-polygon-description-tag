# Dataset contract

## Inclusion rule

The dataset keeps the polygonal OSM features that have at least one description
value that the tool extracted successfully. The value must be a string. It must
be trimmed. It must not be empty after trimming:

- `description=*` for a base description;
- `description:<suffix>=*` for a localized description.

The final artifact boundary is strict. A null value, a blank value, a malformed
value, an untrimmed value, and a failed-extraction value do not count as text.
A malformed localized value or localized container makes its row ineligible.
The DuckDB predicate has the same logic as the Python predicate. The
deduplication, the statistics, the area summaries, and the map inputs use the
DuckDB predicate. The manifest rejection counts and the text-rejection fields
of the generated `stats.json` show the source-level exclusions.

Osmium applies the standard area policy with `area_tags: true`, `linear_tags:
true`, and `--geometry-types polygon`. The tool excludes closed ways with
`area=no`, nodes, open ways, and non-polygon outputs. `area=yes` stays
authoritative. The tool includes `type=multipolygon` and `type=boundary`
relations when the assembly produces a valid Polygon or MultiPolygon.

## Row schema

Each Parquet file uses the versioned GeoParquet schema (`SCHEMA_VERSION = 3`):

| Group | Columns |
| --- | --- |
| Identity | `source_pbf`, `osm_type`, `osm_id`, `osm_url` |
| OSM provenance | `version`, `changeset`, `timestamp` |
| Names | `name`, `localized_names` |
| Descriptions | `description`, `localized_descriptions` |
| Source authority | `tags` |
| Spatial | `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`, `bbox_max_x`, `bbox_max_y`, `geometry` |

`tags` is a complete, sorted list of `{key, value}` records. It has each
original OSM tag and stays authoritative. The name and description columns are
exact derived views for easy queries. The schema uses the list representation
because the Hugging Face Dataset Viewer does not support Arrow map columns. The
tool keeps the localized suffixes exactly. It does not assert that they are
valid language codes.

## Geometry and area

`geometry` contains complete two-dimensional WKB. It has GeoParquet 1.1
metadata and OGC:CRS84 longitude/latitude semantics. `geometry_type` is
`Polygon` or `MultiPolygon`.

`area_m2` is a positive WGS84 geodesic area. The tool normalizes the ring
orientation. It subtracts the holes and includes all MultiPolygon components.
The bounding-box columns cover the complete geometry in source coordinate order.

## Descriptions and words

The generated dataset card reports base descriptions and localized descriptions
separately:

- the number of description values;
- the total number of words, delimited by whitespace;
- the median number of words for each description value;
- the most common exact localized suffixes.

The tool calculates these values from validated Parquet rows. The published
`stats.json` keeps the full suffix counts, the identities of each file, the
rejection counts, and all other machine-readable facts.

The machine-readable report also has dataset-wide polygon geometry facts:

- `area_m2_total_m2`, `area_m2_mean_m2`, `area_m2_min_m2`, `area_m2_p25_m2`,
  `area_m2_median_m2`, `area_m2_p75_m2`, and `area_m2_max_m2`;
- `dataset_bbox` as `[min_lon, min_lat, max_lon, max_lat]`;
- the totals `geometry_vertices_total`, `geometry_rings_total`,
  `geometry_holes_total`, and `multipolygon_components_total`.

The report shows these counts separately: `regional_rows`, distinct global
identities, overlap duplicate rows, regional rows with successful text, and
`unique_polygons_with_successful_nonempty_text`. The report shows the
source/manifest text rejects separately from the persisted artifact rows that
the final text predicate excludes. The area statistics use only the persisted
artifact rows. The field `area_m2_population` records this explicitly. The tool
never sums the area from regional rows silently. The geometry vertices exclude
the repeated closing coordinate of each ring. The generated dataset-card
statistics block shows the same values.

## Reproducibility

Each source has one output Parquet file and one manifest. The manifest
contains these items: the source and output identities, the schema and
transform versions, the tool versions, and the factual counts. The tool writes
and promotes the artifacts atomically. The tool regenerates the statistics and
the dataset card only from validated artifacts and matching manifests. It
writes a file only when the content changes, so the bytes are stable.

## Global identity deduplication

Before publication, and in each global reporting view, the workflow keeps
exactly one canonical row for each `(osm_type, osm_id)` across all regional
extracts. The workflow chooses the canonical row in this order:

1. The highest OSM `version`.
2. The latest `timestamp`.
3. The lexicographically smallest `source_pbf`.
4. A stable row fingerprint, as the final tie-breaker.

The tool rewrites only the affected per-PBF Parquet files and manifests. The
operation is atomic. You can resume it through the local
`.work/dedup-state.json`. The tool never modifies the raw PBF files. The tool
records the deduplicated rows as the factual `duplicate_osm_object` rejection
reason in the manifests and in `stats.json`.

## Map asset

The dataset repository contains the Parquet files and one map asset at
`assets/description_polygon_density.png`. The map is a derived publication
artifact. The tool generates it from the complete validated local dataset. It
is not part of the Parquet schema.

The map has these properties:

* It uses H3 resolution 3.
* It assigns each polygon to a cell by its Shapely geometry centroid.
* It uses a logarithmic colour scale. Sparse areas and dense areas stay visible.
* It counts one canonical row for each globally unique `(osm_type, osm_id)`
  that has successfully extracted trimmed non-empty text.
* It removes the regional overlap duplicates globally. It is not a count of
  regional rows.
* The tool uploads it as part of each per-PBF plan and as part of the final
  metadata plan.
* The publication allowlist includes it under the exact filename
  `assets/description_polygon_density.png`. The tool rejects hidden files,
  temporary files, symlinked files, and unrelated files under `assets/`.
* `publication-state.json` records its SHA-256 and size. A change to the map
  bytes makes the metadata no-op path invalid.
