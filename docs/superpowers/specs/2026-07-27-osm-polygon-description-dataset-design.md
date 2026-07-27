# OSM Polygon Description Dataset Design

## Purpose

Build a public, reproducible dataset of OpenStreetMap polygons carrying at least
one non-empty `description` or `description:<suffix>` tag. The dataset contains
closed ways selected by a public, versioned OSM area policy and successfully assembled
`type=multipolygon` or `type=boundary` relations. Nodes, open ways, and closed
linear ways are out of scope.

The code repository is:

`/Users/noeflandre/osm-polygon-description-tag`

Large local data lives separately under:

`/Volumes/Seagate M3/projects/osm-polygon-description-tag`

The immutable source directory is:

`/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`

The source directory is read-only by contract. The pipeline must never create,
modify, rename, or delete anything inside it.

Public destinations are:

- Code: `https://github.com/NoeFlandre/osm-polygon-description-tag`
- Dataset: `https://huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag`

Initialization and implementation do not authorize a real-data pipeline run,
Hugging Face authentication or upload, or publication to either remote.

## Input Inventory and Output Unit

The source inventory observed on 2026-07-27 contains 386 `.osm.pbf` files and
occupies approximately 77 GB. These are observations, not constants in code or
documentation.

Each source file maps deterministically to exactly one finalized output:

`<data-root>/data/<source-stem>.parquet`

For example:

`afghanistan-latest.osm.pbf` becomes
`data/afghanistan-latest.parquet`.

No global deduplication occurs. Regional extracts may overlap, so the same
`(osm_type, osm_id)` may appear in more than one output file. The dataset card
must disclose this.

## Chosen Architecture

The pipeline streams `osmium export` PostgreSQL COPY records into Python. This
format keeps geometry, OSM metadata, and the complete tag JSON in separate
fields, avoiding collisions between arbitrary OSM tag keys and exported
attribute names. `osmium export` is responsible for area assembly. Python is
responsible for applying the repository's versioned area-policy configuration,
description filtering, normalized fields, geodesic area, bounded Arrow
batches, GeoParquet output, validation, manifests, statistics, and generated
documentation.

This architecture was selected over:

1. Pure PyOsmium, which would require custom area assembly in addition to the
   closed-way classification policy.
2. A full intermediate GeoJSON export, which would add unnecessary disk usage
   and I/O for the current source inventory.

OSM does not publish one exhaustive machine-readable list that unambiguously
classifies every closed way as polygonal or linear. The repository therefore
pins and tests an explicit policy based on the official `osmium-tool` export
example: `area=yes` is polygonal, `area=no` is linear, common linear keys such
as `highway`, `barrier`, and `natural=coastline` are linear, and common area
keys such as `aeroway`, `amenity`, `building`, `landuse`, `leisure`,
`man_made`, and non-coastline `natural` are polygonal. Polygon/boundary
relations remain areas by OSM convention. The exact policy is public dataset
provenance and changes to it require a schema/policy version change and tests.

The external `osmium-tool` binary is an explicit preflight dependency. Python
dependencies and developer tooling are managed with `uv`. Shell pipelines and
shell interpolation are not part of the implementation.

## Record Inclusion

A record is included only when all of the following are true:

1. It comes from a closed way classified as an area by the versioned policy, or
   from a `type=multipolygon` or `type=boundary` relation.
2. `osmium export` successfully emits Polygon or MultiPolygon geometry.
3. At least one tag key is exactly `description` or begins with
   `description:`.
4. At least one matching tag has a non-empty value after trimming whitespace.
5. The record passes the fixed schema and geometry validation.

The original tag values are retained exactly in `tags`. Trimming is used only
to decide whether a value is empty; normalized description columns retain the
original non-empty text.

Language-like suffixes are not normalized or validated as language codes.
For example, `description:pt-BR` uses the exact suffix `pt-BR`. This prevents
the dataset and card from making unsupported claims about language validity.

## Arrow and GeoParquet Schema

Every Parquet file uses one versioned, frozen Arrow schema:

| Column | Type | Meaning |
| --- | --- | --- |
| `source_pbf` | string, non-null | Input basename |
| `osm_type` | string, non-null | `way` or `relation` |
| `osm_id` | int64, non-null | Original OSM identifier |
| `osm_url` | string, non-null | Stable `openstreetmap.org/<type>/<id>` URL |
| `version` | int32, nullable | OSM object version when available |
| `changeset` | int64, nullable | OSM changeset when available |
| `timestamp` | timestamp UTC, nullable | OSM object timestamp when available |
| `description` | string, nullable | Exact base `description` value |
| `localized_descriptions` | map<string, string>, non-null | Exact suffix to exact value |
| `tags` | map<string, string>, non-null | Every original OSM tag |
| `geometry_type` | string, non-null | `Polygon` or `MultiPolygon` |
| `area_m2` | float64, non-null | WGS84 geodesic area in square metres |
| `bbox_min_x` | float64, non-null | Minimum longitude |
| `bbox_min_y` | float64, non-null | Minimum latitude |
| `bbox_max_x` | float64, non-null | Maximum longitude |
| `bbox_max_y` | float64, non-null | Maximum latitude |
| `geometry` | binary, non-null | WKB primary geometry |

The geometry column is WGS84 longitude/latitude and carries valid GeoParquet
1.1 metadata. Areas are computed geodesically rather than in square degrees.
Polygon holes and every component of a multipolygon contribute correctly.

## Components

### Discovery

Discovery enumerates only direct `*.osm.pbf` children of the configured source
directory, sorts them deterministically, derives output names, detects
collisions, and captures available source metadata. It opens no source path for
writing and rejects any output path contained by the source directory.

### Extraction

Extraction invokes a checked `osmium` executable with the versioned export
configuration. It consumes PostgreSQL COPY text from stdout as a bounded
stream, preserving tags as their own JSON field, captures stderr separately,
checks the exit status, and terminates cleanly on downstream failure. No
command is assembled through a shell.

### Transformation

Transformation parses one feature at a time, selects matching description
tags, preserves all tags, creates stable identifiers, converts geometry to
WKB, calculates bounding boxes and geodesic area, and yields fixed-size Arrow
batches. It has no filesystem or subprocess responsibilities.

### Storage and Validation

Storage writes a temporary sibling of the intended final file. Validation
checks the Arrow schema, GeoParquet metadata, row count, non-null constraints,
geometry types, finite positive areas, bounding boxes, and source identity.
Only a validated file is atomically promoted.

### Manifests

Each finalized Parquet has a machine-readable manifest containing:

- manifest schema version;
- input basename, size, modification time, and checksum;
- available source-header metadata;
- code revision and relevant tool/library versions;
- start and completion timestamps;
- included row count;
- emitted-feature and transformation-rejection counts by stable reason code;
- output basename, byte size, checksum, schema version, and GeoParquet version.

Resumption trusts an output only when its manifest matches the current source
identity and its output checksum and validation still pass. A stale result is
rebuilt. A partial result is never treated as complete.

### Reporting and Dataset Card

Reporting reads only validated Parquet files and matching manifests. It writes
a deterministic `stats.json` and replaces marked generated sections in the
Hugging Face `README.md`.

Generated factual statistics include:

- number of source/output files;
- total rows;
- counts by OSM object type and geometry type;
- base-description and localized-description coverage;
- exact description suffix frequencies;
- area distribution summaries;
- source and output byte totals;
- observable transformation-rejection counts by reason;
- data and generation timestamps derived from artifacts.

Handwritten numeric claims are prohibited inside generated sections. The card
also documents schema, loading examples, provenance, OSM attribution, ODbL
obligations, intended uses, limitations, overlap, language-suffix caveats, and
the generation procedure.

The Apache-2.0 repository license covers project code. Derived OpenStreetMap
data remains subject to the Open Database License and must visibly credit
OpenStreetMap contributors. The implementation must not invent a source
provider when the source artifacts do not establish one.

### Publication

Publication is deliberately separate from generation:

1. `publish-plan` validates the entire dataset root and writes/displays the
   exact allowlisted upload set.
2. `publish` requires explicit confirmation and a matching plan identity before
   invoking `hf upload-large-folder`.

Publication never deletes remote files, never follows arbitrary symlinks, and
never runs automatically after a build. Authentication tokens are supplied by
the user environment and are never persisted by this project.

## Public CLI

The console entry point exposes:

- `inspect`: read-only source discovery and preflight.
- `build-one`: build one named PBF output.
- `build-all`: deterministic, resumable orchestration.
- `validate`: validate selected or all finalized outputs.
- `generate-card`: regenerate `stats.json` and the dataset card.
- `publish-plan`: validate and show the exact prospective upload.
- `publish`: separately confirmed execution of an unchanged plan.

Defaults use the approved source and data directories, while every path remains
configurable. Processing concurrency is bounded and explicit, with a
conservative default. CLI help and exit behavior are public contracts.

## Failure Semantics

Infrastructure and integrity errors fail the current PBF and prevent final-file
promotion. These include:

- missing or unsupported tools;
- ambiguous inputs or output collisions;
- subprocess failure or interruption;
- malformed stream records;
- schema or GeoParquet metadata drift;
- checksum or manifest mismatch;
- unsafe path containment;
- documentation/statistics inconsistency.

Expected feature-level failures that reach the Python stream use stable reason
codes and factual counts. Assembly failures internal to `osmium export` are
reported only to the extent exposed by its exit status and diagnostics; the
project must not invent a candidate or rejection count it cannot observe.
Unexpected transformation failures are infrastructure failures, not rejection
counts.

Temporary files remain confined to the output side. Cleanup is limited to
known temporary siblings owned by the current operation.

## Testing Strategy

Implementation follows strict test-driven development: observe a meaningful
RED failure, implement the minimum GREEN behavior, refactor only while green,
and commit in small coherent units.

Tests cover:

- discovery, deterministic naming, and path containment;
- exact description-key and empty-value semantics;
- all-tag preservation and stable URLs;
- fixed Arrow schema and GeoParquet metadata;
- geodesic area for polygons, holes, and multipolygons;
- bounded streaming and batch boundaries;
- atomic promotion and failure cleanup;
- manifest validation and resumability;
- aggregate statistics and deterministic card generation;
- publication allowlists and confirmation identity;
- frozen public CLI help and exit codes.

A tiny committed synthetic OSM fixture contains:

- an eligible closed way;
- a way with `area=no`;
- a closed linear feature;
- a multipolygon with a hole;
- base and localized descriptions;
- unusual but preserved description suffixes;
- empty description values;
- irrelevant nodes and open ways;
- an emitted feature that exercises a stable transformation-rejection path.

Unit tests do not require the 77 GB raw directory. A marked integration test
uses the real `osmium` binary and the synthetic fixture to validate the full
path through GeoParquet, manifest, statistics, and card generation.

## Operational Gates

The phases are deliberately separate:

1. Approved design and implementation plan.
2. TDD implementation using synthetic data only.
3. Independent code and test review.
4. Optional tiny real-source canary, only with explicit user approval.
5. Full local pipeline, only with separate explicit approval.
6. Dataset publication, only with separate explicit approval.

Approval of one phase does not authorize the next. In particular, neither the
implementation agent nor its reviewer may process a real source PBF, write to
the external data root, authenticate to Hugging Face, upload artifacts, push
Git commits, or publish releases without the corresponding explicit approval.

## Initialization Acceptance

Initialization is complete when the repository contains the approved design,
the subsequently approved implementation plan, `uv` project metadata,
public-facing baseline documentation, source/test directory skeletons, and
non-mutating configuration examples, and when the initialization-only checks
pass.

Initialization must not install Homebrew packages, execute the extraction
pipeline, create output Parquet, populate the external data directory, or
contact publication endpoints.
