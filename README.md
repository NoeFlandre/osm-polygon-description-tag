# OSM Polygon Description Tag

Public, reproducible tooling for building one GeoParquet file per OpenStreetMap
PBF extract for polygons carrying `description` or `description:<suffix>` tags.

The dataset includes eligible closed ways selected by a versioned area policy
and successfully assembled `type=multipolygon` or `type=boundary` relations,
with complete original OSM tags, WGS84 geometry, geodesic area, source
identity, validation manifests, and artifact-derived documentation.

## Current status

Implementation tasks 1-12 of the approved plan are complete and verified
locally. No real source PBF has been processed, and no dataset artifact has
been uploaded. Real-source processing, a real-source canary, GitHub
publication, and Hugging Face upload remain separate operational gates.

## Storage boundaries

- Code: `/Users/noeflandre/osm-polygon-description-tag`
- Generated local data:
  `/Volumes/Seagate M3/projects/osm-polygon-description-tag`
- Immutable raw source:
  `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`

The raw source is read-only by contract. It must never be modified, renamed,
deleted, or used as an output or temporary directory.

## Usage

The project uses Python 3.12 and [`uv`](https://docs.astral.sh/uv/).
Extraction depends on `osmium-tool`; publication uses the `hf` CLI.

```bash
uv sync
brew install osmium-tool

# Read-only discovery (safe, never touches source or data roots)
uv run osm-polygon-description-tag inspect

# Build one named PBF output (one basename per invocation)
uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf

# Build all discovered sources, deterministically and resumably
uv run osm-polygon-description-tag build-all

# Validate finalized outputs against manifests and the GeoParquet schema
uv run osm-polygon-description-tag validate

# Regenerate stats.json and the dataset card from validated artifacts
uv run osm-polygon-description-tag generate-card

# Show the exact allowlisted upload plan identity (review before publishing)
uv run osm-polygon-description-tag publish-plan

# Publish (requires exact plan identity confirmation and pre-existing hf auth)
uv run osm-polygon-description-tag publish --plan <identity_sha256> --confirm <identity_sha256>
```

These commands are exposed for documentation. Real-source builds, real-data
card generation, and publication are deliberately separate operational gates
that each require explicit approval. The `publish` command requires the exact
plan identity returned by `publish-plan` and an existing `hf` authentication
in the calling environment; it never calls `hf auth login` and never accepts
a token argument.

## Licensing and attribution

Project code is licensed under Apache-2.0. The derived dataset will contain
OpenStreetMap data and is subject to the Open Database License (ODbL); public
dataset artifacts must credit OpenStreetMap contributors and document their
provenance and limitations.
