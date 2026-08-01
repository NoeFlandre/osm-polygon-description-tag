# Observability

## Purpose

Publish factual dataset-processing metrics to Hugging Face Trackio without
making Trackio a dependency of the extraction state machine.

## Retrospective run

The completed Seagate dataset can be logged without reading raw PBF files:

```bash
uv run osm-polygon-description-tag trackio-report \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
```

The command derives a deterministic per-Parquet curve and a final summary from
validated Parquet files and `stats.json`. It stores local Trackio state below
`<data-root>/logs/trackio` and syncs the run to the public dashboard:

[Open the Trackio dashboard](https://noeflandre-osm-polygon-description-tag-trackio.hf.space/?project=osm-polygon-description-tag&sidebar=hidden).

## Live pipeline runs

The public `run-and-publish` command starts a Trackio run only after preflight
has succeeded. It logs one point per source and a final aggregate point. A
missing Trackio installation, unavailable credentials, or a temporary Space
failure disables metrics and never interrupts extraction, resumability, or
publication.

Trackio data is local-only under the Seagate data root until the configured
Hugging Face Space accepts it; it is never added to an upload plan.
