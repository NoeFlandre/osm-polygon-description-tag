![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

# OSM Polygon Description Tag

This tool extracts OpenStreetMap polygons with complete tags, full GeoParquet
geometry, and geodesic area. It can publish the result to Hugging Face. You
can resume the publication.

The tool reads OpenStreetMap `.osm.pbf` extracts. It keeps each polygon that has a
`description` tag. It writes one validated GeoParquet file for each extract. The file
contains all tags, the full geometry, and the geodesic area. The maintainer
publishes the result as the Hugging Face dataset
[`NoeFlandre/osm-polygon-description-tag`](https://huggingface.co/datasets/NoeFlandre/osm-polygon-description-tag).

## Prerequisites

- Python 3.12 through [uv](https://docs.astral.sh/uv/)
- [`osmium-tool`](https://osmcode.org/osmium-tool/) (`brew install osmium-tool`, or `apt install osmium-tool`)
- [`just`](https://just.systems/) for the recipes (optional)
- To publish only: the Hugging Face `hf` CLI, logged in with write access

## Install

```bash
git clone https://github.com/NoeFlandre/osm-polygon-description-tag
cd osm-polygon-description-tag
uv sync --locked
```

## Usage

Put one or more PBF files in a directory. For example, use a small country
from [Geofabrik](https://download.geofabrik.de/). Use a different, empty
directory for the output.

```bash
export OSM_POLYGON_SOURCE_ROOT=/path/to/pbfs
export OSM_POLYGON_DATA_ROOT=/path/to/data-root

uv run osm-polygon-description-tag inspect        # read-only: list what would be built
uv run osm-polygon-description-tag build-all      # build every extract
uv run osm-polygon-description-tag validate       # check the outputs
uv run osm-polygon-description-tag generate-card  # write stats.json and README.md
uv run osm-polygon-description-tag publish-plan   # dry run: print the upload plan
```

None of these commands uploads data. The command `just run-and-publish` builds
and publishes in one command. You can resume it. It publishes only to the
maintainer's dataset, and it needs write access to that dataset. To stop the
workflow, press Ctrl-C. To resume it, run the same command again.

## Outputs

The tool writes these files under the data root:

```text
data/<extract>.parquet            GeoParquet, one row per polygon
manifests/<extract>.manifest.json build manifest for that file
stats.json, README.md             dataset statistics and card
logs/                             redacted JSONL event log
```

The [dataset contract](docs/dataset-contract.md) describes the column schema and
the guarantees.

## Configuration

| Setting | CLI option | Environment variable |
| --- | --- | --- |
| Immutable PBF directory | `--source-root` | `OSM_POLYGON_SOURCE_ROOT` |
| Generated-data directory | `--data-root` | `OSM_POLYGON_DATA_ROOT` |
| osmium binary | `--osmium` | none |

The tool never writes to the source root. The two roots must not contain each
other. A command that only reads generated data needs only the data root.

## Docker

The Docker image runs as a non-root user. It contains the locked environment
and `osmium-tool`.

```bash
just docker-build
just docker-help
just docker-run /path/to/data-root   # PBFs are read from /path/to/data-root/raw
```

If you do not use `just`, use Compose. `DATA_ROOT` is the data root on the
host. Put the PBF files in its `raw/` directory.

```bash
DATA_ROOT=/path/to/data-root UID="$(id -u)" GID="$(id -g)" \
  docker compose run --rm pipeline run-and-publish \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

The image sets `OSM_POLYGON_SOURCE_ROOT=/data/raw` and
`OSM_POLYGON_DATA_ROOT=/data`. Do not give root options inside the container.
The data root keeps the checkpoints. When you run the command again, it resumes
safely. The tool gets `HF_TOKEN` only at runtime, from the environment or from
an optional `.env` file that git ignores. The tool never copies the token into
the image. Refer to the
[Docker guide](docs/development.md#docker-reproducibility).

## Documentation

MkDocs Material builds the documentation site (`uv run mkdocs serve`). The
site is published from `main` at
[noeflandre.github.io/osm-polygon-description-tag](https://noeflandre.github.io/osm-polygon-description-tag/).
A [codebase presentation](https://noeflandre.github.io/osm-polygon-description-tag/slides/codebase/codebase.html)
is also available.

- [Getting started](docs/getting-started.md)
- [Dataset contract](docs/dataset-contract.md)
- [Operations](docs/operations.md)
- [CLI reference](docs/cli.md)
- [Development](docs/development.md)
- [Architecture](docs/architecture.md)
- [Glossary](docs/glossary.md)
- [Contributing](CONTRIBUTING.md) and [security policy](SECURITY.md)

The tests, the documentation builds, and the CI never read a real PBF corpus.
They never publish to Hugging Face.

## License

The project code uses the Apache-2.0 license. The derived OpenStreetMap data is
© OpenStreetMap contributors. The Open Database License (ODbL) applies to it.

## Citation

If you use this software or its dataset, cite the repository. Use the metadata
in [`CITATION.cff`](CITATION.cff). GitHub uses this file to give formatted
citation downloads through **Cite this repository**.
