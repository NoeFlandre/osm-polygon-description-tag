# OSM Polygon Description Tag

Public, reproducible tooling for building one GeoParquet file per OpenStreetMap
PBF extract for polygons carrying `description` or `description:<suffix>` tags.

The planned dataset includes eligible closed ways and polygon or multipolygon
relations, complete original OSM tags, WGS84 geometry, geodesic area, source
identity, validation manifests, and artifact-derived documentation.

## Current status

The project foundation and implementation plan are initialized. No pipeline has been run.
No real source PBF has been processed, and no dataset artifact has been uploaded.

Implementation is specified in:

- [`docs/superpowers/specs/2026-07-27-osm-polygon-description-dataset-design.md`](docs/superpowers/specs/2026-07-27-osm-polygon-description-dataset-design.md)
- [`docs/superpowers/plans/2026-07-27-osm-polygon-description-dataset.md`](docs/superpowers/plans/2026-07-27-osm-polygon-description-dataset.md)

## Storage boundaries

- Code: `/Users/noeflandre/osm-polygon-description-tag`
- Generated local data:
  `/Volumes/Seagate M3/projects/osm-polygon-description-tag`
- Immutable raw source:
  `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`

The raw source is read-only by contract. It must never be modified, renamed,
deleted, or used as an output or temporary directory.

## Planned workflow

The project uses Python 3.12 and [`uv`](https://docs.astral.sh/uv/). Extraction
will depend on `osmium-tool`; publication will use the `hf` CLI. Installation,
real-data execution, GitHub publication, and Hugging Face upload are outside
the current initialization phase.

The later operational gates are separate:

1. implement and test with synthetic fixtures;
2. independently review code and fresh test evidence;
3. explicitly approve a tiny real-source canary;
4. separately approve the full pipeline;
5. separately approve Hugging Face publication.

Approval of one gate does not authorize the next.

## Licensing and attribution

Project code is licensed under Apache-2.0. The derived dataset will contain
OpenStreetMap data and is subject to the Open Database License (ODbL); public
dataset artifacts must credit OpenStreetMap contributors and document their
provenance and limitations.
