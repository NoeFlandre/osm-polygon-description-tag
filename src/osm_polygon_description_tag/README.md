# Python package architecture

The canonical implementation is organized by operational domain:

- `runtime`: approved paths, resources, logging, and safe cleanup;
- `osm`: deterministic PBF discovery and bounded export;
- `dataset`: schema, transformation, storage, manifests, and reporting;
- `publication`: upload planning, state, execution, and Hub verification;
- `observability`: optional Trackio retrospective and live pipeline metrics;
- `workflow`: preflight, resumable builds, completeness, and lifecycle composition;
- `cli.py`: the stable console entry point.

The domain packages contain the canonical implementation. Existing top-level
modules are explicit compatibility shims that re-export supported names from
those packages; they contain no business logic or mutable state.

New internal code uses canonical domain imports. External callers may continue
using documented compatibility imports.
