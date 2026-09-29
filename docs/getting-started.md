# Getting started

## Prerequisites

The supported environment is Python 3.12 managed by [uv](https://docs.astral.sh/uv/).
The extraction boundary requires `osmium-tool`; publication requires the
Hugging Face `hf` CLI and an authenticated account with write access to the
target dataset.

On macOS:

```bash
brew install uv osmium-tool hf
```

On Debian or Ubuntu, install `osmium-tool` with `apt`, then uv from its
[installer](https://docs.astral.sh/uv/getting-started/installation/).

Clone the repository and install the locked Python environment:

```bash
git clone https://github.com/NoeFlandre/osm-polygon-description-tag
cd osm-polygon-description-tag
uv sync --locked
```

Get input PBFs, for example a small country extract from
[Geofabrik](https://download.geofabrik.de/), into a directory of their own.
Choose a separate, empty directory for generated data, then point the CLI at
both (or pass `--source-root` / `--data-root` on each command):

```bash
export OSM_POLYGON_SOURCE_ROOT=/path/to/pbfs
export OSM_POLYGON_DATA_ROOT=/path/to/data-root
```

Authenticate separately when you are ready to publish:

```bash
hf auth login
```

The pipeline never accepts a token argument and never performs interactive
login itself.

## Inspect safely

Read-only discovery checks the source inventory without creating dataset
artifacts:

```bash
uv run osm-polygon-description-tag inspect
```

The source directory (`OSM_POLYGON_SOURCE_ROOT`) is treated as immutable:
nothing is ever written there.

## Build and validate locally

Build one named source, or all discovered sources, without publishing:

```bash
uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf
uv run osm-polygon-description-tag build-all
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
```

Generated artifacts go under the data root (`OSM_POLYGON_DATA_ROOT`):
`data/*.parquet` with their manifests, `stats.json`, `README.md` (the dataset
card), `logs/` and local state. See the [dataset contract](dataset-contract.md).

`publish-plan` prints the exact files an upload would send, without uploading,
so it works as a dry run.

## Run the complete workflow

The supported one-command operation is:

```bash
just run-and-publish
```

It publishes to the maintainer's dataset, `NoeFlandre/osm-polygon-description-tag`,
and needs write access to it; other users stop at the local build above.

It discovers PBFs deterministically, builds or reuses one artifact per source,
validates each artifact, uploads exact allowlisted files, verifies remote
identities, and updates publication state atomically.

Press Ctrl-C once to stop. The command exits 130 after preserving finalized
artifacts; rerun the same command after the terminal prompt returns to resume.
Do not launch a second instance against the same data root.
