# Getting started

## Prerequisites

The supported environment is Python 3.12, managed by [uv](https://docs.astral.sh/uv/).
The extraction boundary needs `osmium-tool`. Publication needs the Hugging Face
`hf` CLI. It also needs an authenticated account with write access to the
target dataset.

On macOS, install the tools:

```bash
brew install uv osmium-tool hf
```

On Debian or Ubuntu, install `osmium-tool` with `apt`. Then install uv with its
[installer](https://docs.astral.sh/uv/getting-started/installation/).

Clone the repository. Install the locked Python environment:

```bash
git clone https://github.com/NoeFlandre/osm-polygon-description-tag
cd osm-polygon-description-tag
uv sync --locked
```

Put the input PBF files in a directory. For example, use a small country
extract from [Geofabrik](https://download.geofabrik.de/). Choose a different,
empty directory for the generated data. Then tell the CLI where the two
directories are. You can also give `--source-root` and `--data-root` on each
command.

```bash
export OSM_POLYGON_SOURCE_ROOT=/path/to/pbfs
export OSM_POLYGON_DATA_ROOT=/path/to/data-root
```

When you are ready to publish, authenticate separately:

```bash
hf auth login
```

The pipeline does not accept a token argument. It does not do an interactive
login.

## Inspect safely

The read-only discovery step checks the source inventory. It does not create
dataset artifacts.

```bash
uv run osm-polygon-description-tag inspect
```

The tool treats the source directory (`OSM_POLYGON_SOURCE_ROOT`) as immutable.
The tool never writes there.

## Build and validate locally

Build one named source, or all discovered sources. This does not publish.

```bash
uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf
uv run osm-polygon-description-tag build-all
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
```

The tool puts the generated artifacts under the data root
(`OSM_POLYGON_DATA_ROOT`): `data/*.parquet` with their manifests, `stats.json`,
`README.md` (the dataset card), `logs/`, and local state. Refer to the
[dataset contract](dataset-contract.md).

`publish-plan` prints the exact files that an upload sends. It does not upload.
Use it as a dry run.

## Run the complete workflow

The supported one-command operation is:

```bash
just run-and-publish
```

This command publishes to the maintainer's dataset,
`NoeFlandre/osm-polygon-description-tag`. It needs write access to that
dataset. Other users stop at the local build above.

The command does these steps:

1. It discovers the PBF files in a deterministic order.
2. It builds or reuses one artifact for each source.
3. It validates each artifact.
4. It uploads the exact allowlisted files.
5. It verifies the remote identities.
6. It updates the publication state atomically.

To stop the command, press Ctrl-C one time. The command keeps the finalized
artifacts and exits with code 130. When the terminal prompt returns, run the
same command again to resume.

WARNING: Do not start a second instance against the same data root.
