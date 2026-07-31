---
pretty_name: OSM Polygon Description Tag
license: odbl
language:
- multilingual
tags:
- geospatial
- openstreetmap
- geoparquet
---

# OSM Polygon Description Tag

OpenStreetMap polygons with a non-empty `description` or
`description:<suffix>` tag, published as one GeoParquet file per regional PBF
extract. Every row retains the complete original tag map, full Polygon or
MultiPolygon geometry, WGS84 geodesic area, bounding box, and OSM provenance.

<!-- GENERATED:STATS:START -->
<!-- GENERATED:STATS:END -->
<!-- GENERATED:H3_MAP:START -->
![H3 density of description-tagged polygons](assets/description_polygon_density.png)
<!-- GENERATED:H3_MAP:END -->

## What is included

- Tagged closed ways that OSM classifies as areas, excluding `area=no`.
- Successfully assembled `type=multipolygon` and `type=boundary` relations.
- Exact base and localized descriptions and names.
- Complete original OSM tags, full WKB geometry, `area_m2`, and bounding boxes.

Nodes, open ways, undescribed features, and failed polygon assemblies are not
included. Regional extracts can overlap, so the same OSM object may appear in
more than one file.

## Schema

- **Identity:** `source_pbf`, `osm_type`, `osm_id`, `osm_url`
- **OSM provenance:** `version`, `changeset`, `timestamp`
- **Convenience text fields:** `name`, `localized_names`, `description`,
  `localized_descriptions`
- **Authoritative source tags:** `tags`
- **Spatial fields:** `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`,
  `bbox_max_x`, `bbox_max_y`, `geometry`

`geometry` is WKB with GeoParquet 1.1 metadata and OGC:CRS84 longitude/latitude
semantics. The `tags` map is authoritative; convenience text fields are exact
derived views.

## Load the data

```python
import pyarrow.parquet as pq

table = pq.read_table("data/<region>-latest.parquet")
```

```python
import geopandas as gpd

gdf = gpd.read_parquet("data/<region>-latest.parquet")
```

## Methodology

`osmium export` applies standard OSM area handling and emits polygon geometry
only. The pipeline retains features with at least one exact non-empty
description tag, computes geodesic WGS84 area with holes and multipolygon
components included, validates GeoParquet and manifest identities, and writes
artifacts atomically.

All displayed statistics are generated from validated Parquet files and their
matching manifests. No counts are handwritten.

## Limitations

- Suffixes such as `en` or `pt-BR` are preserved exactly but are not validated
  as language codes.
- Text comes directly from OpenStreetMap and may vary in quality, language,
  formatting, and completeness.
- Regional overlap means this is not a globally deduplicated table.
- Geometry and tags reflect the source extracts at their recorded OSM
  timestamps.

## License and attribution

Derived data is © OpenStreetMap contributors and available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/) (ODbL).
Users and redistributors must comply with its attribution and share-alike
requirements. Pipeline code is Apache-2.0.

## Reproducibility

The public source repository contains the versioned extraction policy,
deterministic reporting code, validation contracts, and the stoppable,
resumable `just run-and-publish` workflow.
