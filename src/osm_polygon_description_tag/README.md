# Python package architecture

The canonical implementation is organized by operational domain:

- `runtime`: approved paths, resources, logging, and safe cleanup;
- `osm`: deterministic PBF discovery and bounded export;
- `dataset`: schema, transformation, storage, manifests, and reporting;
- `publication`: upload planning, state, execution, and Hub verification;
- `workflow`: preflight, resumable builds, completeness, and lifecycle composition;
- `cli.py`: the stable console entry point.

The domain packages are being introduced incrementally. Until each migration is complete, the
existing top-level modules remain the implementation and supported import paths. After migration,
those top-level modules become explicit compatibility shims that re-export supported names from
the canonical package. The existing `osm_polygon_description_tag.publication` module is replaced
atomically by its same-named package so imports are never ambiguously split between both forms.

New internal code should use canonical domain imports once the corresponding API has moved.
External callers may continue using the documented compatibility imports. Compatibility shims do
not own business logic or mutable state.
