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

One GeoParquet file per OpenStreetMap regional extract, containing every polygon
or multipolygon that carries at least one non-empty `description` or
`description:<suffix>` tag. Closed ways are selected by a versioned, public area
policy; `type=multipolygon` and `type=boundary` relations are included when area
assembly succeeds.

<!-- GENERATED:STATS:START -->
<!-- GENERATED:STATS:END -->

## Schema

Each Parquet file uses one versioned Arrow schema with a WKB `geometry` column
carrying valid GeoParquet 1.1 metadata (OGC:CRS84 longitude/latitude). Columns
include source identity (`source_pbf`, `osm_type`, `osm_id`, `osm_url`), OSM
metadata when available (`version`, `changeset`, `timestamp`), the exact base
`description`, the exact `localized_descriptions` suffix-to-value map, the full
original `tags`, geometry type, positive geodesic `area_m2`, bounding-box
columns, and the WKB geometry.

## Loading

```python
import pyarrow.parquet as pq

table = pq.read_table("data/<source-stem>.parquet")
```

```python
import geopandas as gpd

df = gpd.read_parquet("data/<source-stem>.parquet")
```

## Source and methodology

Inputs are direct `*.osm.pbf` children of an immutable, read-only source
directory. Each source maps deterministically to exactly one output. Geometry is
assembled by `osmium export` (PostgreSQL COPY mode); the repository's versioned
`config/osmium-export.json` pins the closed-way area/linear policy that is the
public provenance for closed-way classification. OSM does not publish one
exhaustive machine-readable polygon-key standard, so this policy is named
explicitly rather than claimed as a universal classifier.

Areas are computed geodesically (WGS84) with correct hole and multipolygon
handling after ring normalization. Outputs are written atomically and validated
before promotion; manifests record source/output identity, checksums, tool and
library versions, and factual feature/rejection counts.

## Attribution and license

Project code is Apache-2.0. Derived OpenStreetMap data is © OpenStreetMap
contributors and licensed under the Open Database License (ODbL). Public dataset
artifacts must visibly credit OpenStreetMap contributors. When using or
redistributing the data, you must comply with the ODbL obligations, including
attributing OpenStreetMap and keeping any derived database under a compatible
license. The implementation does not invent a source provider when the source
artifacts do not establish one.

## Intended uses

Geospatial research, enrichment, documentation discovery, quality inspection,
and reproducible downstream pipelines that need described polygons with stable
identifiers and exact original tags.

## Limitations

- `description:<suffix>` suffixes are preserved verbatim and are **not**
  validated as language codes. Suffixes such as `pt-BR` are kept exactly; the
  dataset and card make no claim that every suffix is a valid language tag.
- Regional extracts overlap; the same `(osm_type, osm_id)` may appear in more
  than one output file. No global deduplication is performed.
- Feature-level rejections use stable reason codes and factual counts derived
  only from observable transformation outcomes; counts that osmium does not
  expose (for example internal assembly failures) are not invented.
- Geometry is WGS84 longitude/latitude; ring orientation is normalized for
  geodesic area, but the data otherwise reflects the source.

## Reproducibility

Run `inspect` (read-only), then `build-all`, `validate`, and `generate-card`.
Build, real-source processing, and publication are separate operational gates
that each require explicit approval.

## Contact

See the project source repository for issues and provenance.
