# Geography subpackage

## Purpose

Produce the deterministic H3 hexagon density map of the description-tagged
polygons and integrate it as a dataset-card artifact and a publication
asset.

## Map contract

* The map is published exactly once at
  `assets/description_polygon_density.png` relative to the dataset
  repository root.
* The map counts every dataset row exactly once. Regional overlap is
  preserved: the same OSM object appearing in two regional extracts is
  counted as two dataset rows.
* H3 resolution 3 is used.
* The colour scale is logarithmic (`matplotlib.colors.LogNorm`).
* Natural Earth 110m landmasses are bundled in the package and drawn in
  beige over the blue ocean. Rendering never downloads basemap data.
* The map cache identity is derived from finalized Parquet hashes, the H3
  resolution, renderer revision, and basemap hash. README-only regeneration
  reuses the existing PNG; data or rendering-input changes invalidate it.
* The map caption reports the total dataset row count and the number
  of occupied H3 cells, derived from the aggregation.
* Re-rendering identical input produces byte-identical PNGs and
  preserves the existing file mtime.

## Centroid policy

The H3 cell is computed from the Shapely geometry centroid of the full
WKB geometry, not the bounding-box centre. The centroid is validated
for finiteness and range before H3 assignment. Malformed WKB, invalid
geometry, null geometry, non-finite coordinates, and out-of-range
coordinates raise a descriptive error and are never silently skipped.

## Submodules

* `h3_policy` owns coordinate validation, H3 v4 cell assignment, the
  antimeridian-safe cell ring conversion, and the stable H3 v4
  `(lon, lat)` boundary ordering used by the renderer.
* `parquet_inputs` owns the column-pruned `iter_batches` reader and
  the centroid-to-H3 stream. The full dataset is never read into
  memory: peak memory is bounded by the batch size and the number
  of distinct H3 cells, not the total row count.
* `aggregation` is a thin pure function that walks every Parquet
  under `data/` and returns a sorted `{h3_cell: count}` mapping.
* `basemap` owns loading and drawing the bundled Natural Earth 110m
  landmasses. It never performs network I/O.
* `rendering` owns the deterministic PNG output. It is a pure
  function of the cell counts, the bundled land reference, and the fixed
  visual constants. Land is drawn beneath H3 cells so geographic context
  remains visible at sparse densities.
* `card` owns the dataset-card marker block (`<!-- GENERATED:H3_MAP:START/END -->`)
  and the byte-stable substitution that keeps the surrounding
  handwritten prose verbatim across regenerations.

## Non-responsibilities

This subpackage does not discover PBFs, perform osmium exports, build
Parquet files, or manage publication plans. The map is a derived
publication artifact only; it is not added to the Parquet schema.

## Public API

Import stable APIs from `osm_polygon_description_tag.dataset.geography`,
for example `aggregate_h3_density`, `render_density_map`,
`H3_MAP_ASSET_RELATIVE_PATH`, `H3_MAP_START_MARKER`, and
`H3_MAP_END_MARKER`.

## Allowed dependencies

The `dataset` package, h3, matplotlib, pyarrow, shapely, and the
Python standard library. Matplotlib uses the non-interactive `Agg`
backend suitable for CI and macOS terminal execution.

## Tests

Run `uv run pytest tests/unit/dataset/test_geography_*.py -q` and
`uv run pytest tests/integration/test_three_run_map_lifecycle.py -q`.
