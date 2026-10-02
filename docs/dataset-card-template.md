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

This dataset has OpenStreetMap polygons. Each polygon has a successfully
extracted trimmed non-empty `description` tag or `description:<suffix>` tag.
The dataset has one GeoParquet file for each regional PBF extract. Each row
keeps the complete original tag map, the full Polygon or MultiPolygon geometry,
the WGS84 geodesic area, the bounding box, and the OSM provenance.

Source repository: [github.com/NoeFlandre/osm-polygon-description-tag](https://github.com/NoeFlandre/osm-polygon-description-tag).

See the pipeline metrics in the [Trackio dashboard](https://noeflandre-osm-polygon-description-tag-trackio.static.hf.space/?project=osm-polygon-description-tag&sidebar=hidden).

Read the [dataset presentation](https://noeflandre.github.io/osm-polygon-description-tag/slides/dataset/dataset.html). It gives a short visual overview of the snapshot, the methodology, and the findings.

The map and the generated counts describe canonical globally unique
`(osm_type, osm_id)` polygons. Each polygon has successfully extracted trimmed
non-empty description text. The card reports the regional/raw rows and the
overlap duplicates separately.

<!-- GENERATED:H3_MAP:START -->
![H3 density of canonical globally unique `(osm_type, osm_id)` polygons with successfully extracted trimmed non-empty description text](assets/description_polygon_density.png)
<!-- GENERATED:H3_MAP:END -->
The map is a hexbin density of each canonical globally unique `(osm_type,
osm_id)` polygon with successfully extracted trimmed non-empty description
text. It uses H3 resolution 3. It is drawn from the geometry centroid of each
canonical row on a logarithmic scale. The map removes the regional overlap
duplicates globally. It is not a count of regional rows. A lighter cell has more
polygons.
<!-- GENERATED:STATS:START -->
<!-- GENERATED:STATS:END -->

## Terminology

- **Closed way**: an OSM way with the same identifier for its first node and its
  last node. `osmium export` emits it as an area when its tags mark it as a
  polygon feature.
- **Relation**: an OSM object (here `type=multipolygon` or `type=boundary`) that
  groups several ways into one logical feature. The tool keeps it when it
  assembles into a valid polygon.
- **Polygon**: an area geometry with a single outer ring.
- **MultiPolygon**: a geometry of one or more disjoint Polygon parts. The tool
  produces it for assembled multipolygon and boundary relations.
- **Base description**: the exact text of the `description=*` tag on a feature.
- **Localized description**: the exact text of a `description:<suffix>=*` tag
  with a suffix. The tool keeps the suffix exactly. It does not validate the
  suffix as a language code.

## What is included

- Tagged closed ways that OSM classifies as areas. The tool excludes `area=no`.
- Successfully assembled `type=multipolygon` and `type=boundary` relations.
- Exact base and localized descriptions and names. The kept description values
  are trimmed, non-null strings. Each row needs at least one valid description
  value.
- Complete original OSM tags, full WKB geometry, `area_m2`, and bounding boxes.

The dataset does not include these items: nodes, open ways, undescribed
features, malformed or blank description values, and failed polygon assemblies.
The tool removes cross-region duplicates globally before publication. The
manifest rejection counts show the source-level exclusions.

## Schema

- **Identity:** `source_pbf`, `osm_type`, `osm_id`, `osm_url`
- **OSM provenance:** `version`, `changeset`, `timestamp`
- **Convenience text fields:** `name`, `localized_names`, `description`,
  `localized_descriptions`
- **Authoritative source tags:** `tags`
- **Spatial fields:** `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`,
  `bbox_max_x`, `bbox_max_y`, `geometry`

`geometry` is WKB with GeoParquet 1.1 metadata and OGC:CRS84 longitude/latitude
semantics. The `tags` key/value list is authoritative. The convenience text
fields are exact derived views.

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

`osmium export` applies standard OSM area handling. It emits only polygon
geometry. The pipeline does these steps:

1. It keeps the features that have at least one successfully extracted trimmed
   non-empty description tag.
2. It computes the geodesic WGS84 area. The area includes the holes and the
   multipolygon components.
3. It validates the GeoParquet and manifest identities.
4. It writes the artifacts atomically.

The global statistics and the map use one deterministic canonical row for each
`(osm_type, osm_id)`. The card reports the regional rows, the overlap
duplicates, and the source rejection categories separately.

The tool generates all displayed statistics from validated Parquet files and
their matching manifests. No counts are handwritten.

## Limitations

- The tool keeps suffixes such as `en` or `pt-BR` exactly. It does not validate
  them as language codes.
- The text comes directly from OpenStreetMap. The quality, language, format, and
  completeness can be different.
- The tool removes cross-region overlaps globally by `(osm_type, osm_id)`
  before publication.
- The geometry and the tags show the source extracts at their recorded OSM
  timestamps.

## License and attribution

The derived data is © OpenStreetMap contributors. The
[Open Database License](https://opendatacommons.org/licenses/odbl/) (ODbL)
applies to it. Users and redistributors must obey its attribution and
share-alike requirements. The pipeline code uses the Apache-2.0 license.

## Reproducibility

The public source repository contains the versioned extraction policy, the
deterministic reporting code, the validation contracts, and the
`just run-and-publish` workflow. You can stop and resume this workflow.

## Citation

If you use this software or its dataset, cite the repository. Use the metadata
in [`CITATION.cff`](https://github.com/NoeFlandre/osm-polygon-description-tag/blob/main/CITATION.cff).
GitHub gives formatted citation downloads through **Cite this repository**.
