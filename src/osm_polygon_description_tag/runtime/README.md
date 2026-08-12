# Runtime package

## Purpose

Provide the canonical boundary for process-wide runtime support.

## Responsibilities

This package owns approved path configuration, packaged-resource lookup, operational logging,
interactive terminal presentation, and safe cleanup of abandoned application-owned temporary
files. It also provides the shared UTC timestamp helper used by dataset reporting and workflow
manifests.

## Non-responsibilities

This package does not discover OSM inputs, build dataset records, publish artifacts, or coordinate
the workflow.

## Public API

Import canonical APIs from the package boundary:

```python
from osm_polygon_description_tag.runtime import (
    Paths,
    RunLogger,
    TerminalPresenter,
    cleanup_stale_owned_temps,
    osmium_export_config,
)
```

The package exports `Paths`, `UnsafePathError`, `RunLogger`, `TerminalPresenter`,
`configure_rotation`, the packaged resource locators, project checkout locators, and
`cleanup_stale_owned_temps`.

## Allowed dependencies

Python's standard library and static package resources.

## Data flow and side effects

Configuration validates source and data roots. Resource APIs locate immutable packaged files and
may inspect the project checkout revision. Logging writes operational event streams. Rich and tqdm
presentation is restricted to interactive stderr so stdout remains the final JSON result. Cleanup
removes only recognized stale temporary files when a newer finalized target exists.

## Safety and determinism invariants

Paths remain explicitly validated, packaged resources are immutable, and cleanup stays confined to
identified application-owned files.

## Tests

Runtime unit tests cover configuration, resources, logging, terminal presentation, and stale
temporary-file cleanup; contract tests preserve existing import paths.
