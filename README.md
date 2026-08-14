![OSM Polygon Description Tag dataset hero](assets/dataset-card-hero.png)

# OSM Polygon Description Tag

Reproducible OpenStreetMap polygon extraction with complete tags, full
GeoParquet geometry, geodesic area, and resumable Hugging Face publication.

The public documentation site is built with MkDocs Material:

```bash
uv run mkdocs serve
```

It is published automatically from `main` with GitHub Pages at
[noeflandre.github.io/osm-polygon-description-tag](https://noeflandre.github.io/osm-polygon-description-tag/).
The deployment workflow builds the strict site from the locked uv environment.

Read the [codebase presentation](https://noeflandre.github.io/osm-polygon-description-tag/slides/codebase/codebase.html)
for a concise visual overview of the architecture, contracts, and workflow.

Read the documentation:

- [Getting started](docs/getting-started.md)
- [Dataset contract](docs/dataset-contract.md)
- [Operations](docs/operations.md)
- [CLI reference](docs/cli.md)
- [Development](docs/development.md)
- [Architecture](docs/architecture.md)

## Quick start

```bash
cd /Users/noeflandre/osm-polygon-description-tag
uv sync --locked
just run-and-publish
```

The workflow is stoppable with Ctrl-C and resumable by rerunning the same
command after the terminal prompt returns.

### Reproducible Docker runtime

The repository also provides a non-root Docker image with the locked Python
environment and the real `osmium-tool` binary. Build and inspect its harmless
default command without mounting data:

```bash
just docker-build
just docker-help
```

Run the complete resumable build-and-publish workflow with one explicit
command. It builds or reuses the cached image, mounts the generated-data root
at `/data`, and mounts `/data/raw` read-only:

```bash
just docker-run "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
```

The host data root retains checkpoints and publication state, so `Ctrl-C` and
rerunning the same command resumes safely. `HF_TOKEN` is passed through only
at runtime and is never copied into the image. See the [Docker reproducibility
guide](docs/development.md#docker-reproducibility) for test/check commands and
the container boundary.

## Boundaries

- Code: `/Users/noeflandre/osm-polygon-description-tag`
- Immutable raw PBFs: `/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw`
- Generated data: `/Volumes/Seagate M3/projects/osm-polygon-description-tag`
- Hugging Face dataset: `NoeFlandre/osm-polygon-description-tag`

The raw source is read-only. Tests, documentation builds, and CI do not access
the real PBF corpus or publish to Hugging Face.

Project code is Apache-2.0. Derived OpenStreetMap data is © OpenStreetMap
contributors and subject to the Open Database License (ODbL).

## Citation

If you use this software or its dataset, please cite the repository using the
metadata in [`CITATION.cff`](CITATION.cff). GitHub uses this file to provide
formatted citation downloads through **Cite this repository**.
