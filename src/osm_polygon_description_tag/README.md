# Python package architecture

The canonical implementation is organized by operational domain:

- `runtime`: approved paths, resources, logging, and safe cleanup;
- `osm`: deterministic PBF discovery and bounded export;
- `dataset`: schema, transformation, storage, manifests, and reporting;
- `publication`: upload planning, state, execution, and Hub verification;
- `observability`: optional Trackio snapshot and live pipeline metrics;
- `workflow`: preflight, resumable builds, completeness, and lifecycle composition;
- `cli.py`: the stable console entry point.

The domain packages contain the implementation. The package root holds only
the console modules; import everything else from its domain package.
