# Getting started

## Prerequisites

The supported environment is Python 3.12 managed by [uv](https://docs.astral.sh/uv/).
The extraction boundary requires `osmium-tool`; publication requires the
Hugging Face `hf` CLI and an authenticated account with write access to the
target dataset.

```bash
brew install uv
brew install osmium-tool
brew install hf
```

Install the locked Python environment from the repository:

```bash
cd /Users/noeflandre/osm-polygon-description-tag
uv sync --locked
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

The immutable source directory is:

```text
/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw
```

## Build and validate locally

Build one named source, or all discovered sources, without publishing:

```bash
uv run osm-polygon-description-tag build-one afghanistan-latest.osm.pbf
uv run osm-polygon-description-tag build-all
uv run osm-polygon-description-tag validate
uv run osm-polygon-description-tag generate-card
```

Generated artifacts belong under:

```text
/Volumes/Seagate M3/projects/osm-polygon-description-tag
```

## Run the complete workflow

The supported one-command operation is:

```bash
just run-and-publish
```

It discovers PBFs deterministically, builds or reuses one artifact per source,
validates each artifact, uploads exact allowlisted files, verifies remote
identities, and updates publication state atomically.

Press Ctrl-C once to stop. The command exits 130 after preserving finalized
artifacts; rerun the same command after the terminal prompt returns to resume.
Do not launch a second instance against the same data root.
