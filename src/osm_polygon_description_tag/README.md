# Python package architecture

The canonical implementation is organized by operational domain:

- `runtime`: approved paths, resources, logging, and safe cleanup;
- `osm`: deterministic PBF discovery and bounded export;
- `dataset`: schema, transformation, storage, manifests, and reporting;
- `publication`: upload planning, state, execution, and Hub verification;
- `observability`: optional Trackio snapshot and live pipeline metrics;
- `workflow`: preflight, resumable builds, completeness, and lifecycle composition;
- `cli.py`: the stable console entry point (Typer declarations, global options, exit codes);
- `cli_handlers.py`: the command handlers that `cli.py` dispatches to;
- `language_cli.py`: the `language` command group, backed by
  `workflow/language_workflow.py` (local detection), `workflow/grid_workflow.py`
  and `workflow/grid_transport.py` (Grid'5000 runs), and
  `workflow/publication_workflow.py` (language export and publication).

The domain packages contain the implementation. There are no compatibility
re-exports: import each name from the module that defines it.
