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

## Stoppable, resumable publication

The recommended entry point for the full per-PBF build + publish flow is
`run-and-publish`. The command is stoppable (Ctrl-C exits 130) and fully
resumable. Re-running it after any interruption is safe and idempotent:
nothing is rebuilt if the local artifact is already complete and verified.

```bash
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Behavior:

- **Stoppable**: Ctrl-C exits 130. The active osmium child is terminated,
  in-flight temporary files are removed, and prior artifacts are kept
  intact. The next invocation resumes from the last verified state.
- **Resumable**: rerunning the same command is safe. The orchestrator
  discovers PBFs deterministically, reuses every local artifact whose
  manifest agrees with the current source/output identity and runtime
  versions, and uploads only the unknown or changed items.
- **Per-PBF plan**: each PBF is published as a single, exact allowlisted
  upload containing four files -- `data/<stem>.parquet`,
  `manifests/<stem>.manifest.json`, `README.md`, and `stats.json`. Each
  PBF is individually verified against the Hub before its state is
  recorded.
- **Local resumable cache**: the uploader may create
  `<data-root>/.cache/huggingface/upload/...` during a run. This directory
  is local uploader state, is required for resumable uploads, is never
  included in any upload plan or upload command, and is never deleted by
  the orchestrator. The allowlist accepts the exact
  `.cache/huggingface` layout (real directory, not a symlink) and rejects
  any unrelated hidden top-level entry.
- **Final metadata**: `README.md` and `stats.json` are uploaded as a
  single, independently resumable final step. Their identities
  (SHA-256, size, plan identity) are recorded in
  `publication-state.json` as a separate `metadata` block that is
  written atomically only after Hub verification succeeds. If the final
  metadata upload fails, the next invocation retries exactly the
  metadata upload -- per-PBF artifacts are not re-uploaded. When the
  third run finds both per-PBF and metadata state complete and current,
  it performs zero builds, zero uploads, and zero verifier calls and
  preserves all local bytes.
- **Hardened preflight**: the command refuses to start if any of the
  following fails:
  - source root is readable, data root is writable;
  - the osmium binary is on `PATH` and reports a real version;
  - the `hf` CLI is on `PATH` and reports an authenticated identity;
  - the Hub API confirms the caller's **write** permission for the
    target dataset (a non-mutating `auth_check(write=True,
    repo_type="dataset")` call). Any denial aborts before any PBF is
    opened or any generated artifact is created.
- **Dataset guarantees**: closed ways + qualifying `multipolygon` /
  `boundary` relations; full Polygon / MultiPolygon WKB; GeoParquet
  CRS84 metadata; WGS84 geodesic `area_m2`; bounding boxes; `description`
  + `description:<suffix>`; every original OSM tag; immutable raw source;
  bounded-memory processing.

## Output schema

The output GeoParquet is `SCHEMA_VERSION = 2` with the following columns in
order:

- `source_pbf`, `osm_type`, `osm_id`, `osm_url`
- `version`, `changeset`, `timestamp` (OSM provenance)
- `name` (nullable), `localized_names` (`name:<suffix>` to value map)
- `description` (nullable), `localized_descriptions` (`description:<suffix>` to value map)
- `tags` (full original OSM tag map, byte-faithful)
- `geometry_type`, `area_m2`, `bbox_min_x`, `bbox_min_y`, `bbox_max_x`, `bbox_max_y`, `geometry`

The `tags` column is authoritative. `name` and `description` are derived from
`tags` for query convenience and stay in lock-step with the source map for
every record.

Closed-way selection uses osmium's general area handling: `area_tags: true`,
`linear_tags: true`, plus `--geometry-types polygon` on the export command.
The exact policy is versioned in `config/osmium-export.json` and pinned in
each manifest as a SHA-256 provenance.

## Operational logs and progress

`run-and-publish` writes a typed event stream to
`<data-root>/logs/run-and-publish.jsonl` (redacted JSONL) and a human
line on stderr. The log file rotates atomically at 10 MiB with five
backups, using same-directory hard-link staging and `os.replace`. The
logs directory is allowlisted locally but never included in any upload
plan.

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

# Stoppable, resumable per-PBF build + publish (recommended)
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag

# Lower-level publish (requires exact plan identity confirmation and pre-existing hf auth)
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
