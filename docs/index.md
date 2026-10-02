# OSM Polygon Description Tag

This project is a reproducible pipeline. It builds one validated GeoParquet
file for each OpenStreetMap PBF extract. The file contains the described
polygons with complete source tags, full geometry, geodesic area, and
provenance.

## What this project delivers

- Closed ways, selected with standard OSM area handling.
- `type=multipolygon` and `type=boundary` relations, when polygon assembly
  succeeds.
- Base names and descriptions, and localized names and descriptions. The tool
  keeps every original tag.
- Full Polygon or MultiPolygon WKB, WGS84 geodesic `area_m2`, and bounding
  boxes.
- Deterministic manifests, statistics, and dataset-card generation. The
  Hugging Face publication can be stopped.

## Start here

The rendered documentation is at
[noeflandre.github.io/osm-polygon-description-tag](https://noeflandre.github.io/osm-polygon-description-tag/).
The site is built again automatically when `main` changes.

1. Do the steps in [Getting started](getting-started.md) to install the toolchain.
2. Read the [Dataset contract](dataset-contract.md) before you use the rows.
3. Use [Operations](operations.md) for the storage, resume, and publication rules.
4. Use the [CLI reference](cli.md) for the behavior of the commands.
5. Use the [Glossary](glossary.md) for the project terms.

The tool generates the live dataset card and the `stats.json` file from the
validated published Parquet files. This documentation has no hand-written
dataset counts. It has no claims about freshness.

## Project boundaries

The source root holds the immutable raw PBF files (`--source-root` or
`OSM_POLYGON_SOURCE_ROOT`). A separate data root holds the generated local
artifacts (`--data-root` or `OSM_POLYGON_DATA_ROOT`).

The raw source is read-only. The tests, the documentation builds, and the CI
do not read the real PBF corpus. They do not publish to Hugging Face.

## License

The project code uses the Apache-2.0 license. The derived OpenStreetMap data is
© OpenStreetMap contributors. The
[Open Database License](https://opendatacommons.org/licenses/odbl/) applies to
it.
