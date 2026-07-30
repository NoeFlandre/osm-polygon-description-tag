# OSM package

## Purpose

Provide the canonical boundary for deterministic OpenStreetMap source ingestion.

## Responsibilities

Discover immutable source PBFs without modifying or following indirect inputs, and stream bounded
`osmium export` records.

## Non-responsibilities

This package does not transform exported records, write GeoParquet, publish files, or manage
workflow state.

## Public API

Import the canonical APIs from the package boundary:

```python
from osm_polygon_description_tag.osm import discover_sources, stream_export

for source in discover_sources(source_root):
    for record in stream_export(source.path, export_config):
        process(record)
```

`Source`, `discover_sources`, `STDERR_CAP_BYTES`, `OsmiumExportError`, `ExportRecord`,
`export_command`, `parse_copy_record`, `iter_records`, `stream_export`, and `osmium_version` are
public. The former top-level `discovery` and `extraction` modules remain compatibility aliases.

## Allowed dependencies

The `runtime` package and Python's standard library.

## Data flow and side effects

Discovery is read-only: it enumerates direct, regular `*.osm.pbf` children in deterministic order
and records their immutable identity metadata. Extraction constructs the exact no-shell argument
array

```text
osmium export SOURCE --output-format pg --config CONFIG --geometry-types polygon --output -
```

and passes it to `subprocess` with `shell=False`. Records are yielded as a bounded stream and
retained stderr is capped for diagnostics. This package does not transform records, write Parquet,
publish files, or manage workflow state.

## Safety and determinism invariants

Discovery order and source identities remain deterministic, and subprocess output remains bounded
and validated before use.

## Tests

OSM unit tests cover discovery and extraction streams; integration tests cover the external
`osmium` boundary, and contract tests preserve existing import paths.
