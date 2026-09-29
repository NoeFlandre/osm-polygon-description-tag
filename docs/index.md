# OSM Polygon Description Tag

A reproducible pipeline for building one validated GeoParquet file per
OpenStreetMap PBF extract, containing described polygons with complete source
tags, full geometry, geodesic area, and provenance.

## What this project delivers

- Closed ways selected with standard OSM area handling.
- `type=multipolygon` and `type=boundary` relations when polygon assembly
  succeeds.
- Base and localized names and descriptions, with every original tag retained.
- Full Polygon or MultiPolygon WKB, WGS84 geodesic `area_m2`, and bounding
  boxes.
- Deterministic manifests, statistics, dataset-card generation, and
  stoppable Hugging Face publication.

## Start here

The rendered documentation is available at
[noeflandre.github.io/osm-polygon-description-tag](https://noeflandre.github.io/osm-polygon-description-tag/)
and is rebuilt automatically when `main` changes.

1. Follow [Getting started](getting-started.md) to install the toolchain.
2. Read the [Dataset contract](dataset-contract.md) before consuming rows.
3. Use [Operations](operations.md) for storage, resume, and publication rules.
4. Use the [CLI reference](cli.md) for command behavior.

The live dataset card and artifact-derived `stats.json` are generated from the
validated published Parquet files. This documentation intentionally contains
no hand-written dataset counts or freshness claims.

## Project boundaries

Immutable raw PBFs live in the source root (`--source-root` or
`OSM_POLYGON_SOURCE_ROOT`). Generated local artifacts live in a separate data
root (`--data-root` or `OSM_POLYGON_DATA_ROOT`).

The raw source is read-only. Tests, documentation builds, and CI do not read
the real PBF corpus or publish to Hugging Face.

## License

Project code is Apache-2.0. Derived OpenStreetMap data is © OpenStreetMap
contributors and is subject to the [Open Database License](https://opendatacommons.org/licenses/odbl/).
