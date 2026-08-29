# COPY Parser and Transform Performance Design

**Date:** 2026-08-29

## Goal

Reduce wall-clock time and allocation pressure in the real-data PBF-to-GeoParquet build path without changing any dataset, CLI, resumability, or error contracts.

The first benchmark target is the 107 MB `afghanistan-latest.osm.pbf` source from the immutable raw-data root, written to an isolated temporary data root. The existing end-to-end baseline is 434.84 seconds for 2,147,728 emitted features and 184 included rows. The `osmium export` subprocess alone takes 27.22 seconds, so Python parsing and transformation are the dominant measured cost.

## Scope

This pass changes only the hot path that handles records emitted by `osmium`:

- `src/osm_polygon_description_tag/osm/extraction.py`
  - Make COPY unescaping return the original immutable bytes when no escape is present.
  - Move the supported escape mapping out of the per-byte loop.
  - Pass the already-unescaped bytes directly to `json.loads` for tag objects.
- `src/osm_polygon_description_tag/dataset/transform.py`
  - Scan only matching localized keys and sort only those matches, instead of sorting every tag for every record.
- Focused tests in `tests/unit/osm/test_extraction_helpers.py` and `tests/unit/dataset/test_transform.py`.

No new dependency, CLI option, worker pool, output format, storage algorithm, or publication behavior is introduced. Multi-PBF parallelism and Parquet rewrite changes remain separate follow-up designs because they have different resource and correctness risks.

## Design

### COPY parsing

`_copy_unescape` keeps the current escape semantics for `\\t`, `\\n`, `\\r`, `\\b`, `\\f`, `\\v`, `\\\\`, unknown escapes, and trailing backslashes. It first checks for a backslash using the C-level bytes operation. If none exists, it returns the input bytes unchanged. If one exists, it uses a module-level mapping and the existing byte-by-byte state machine.

`_parse_tags` retains the existing null handling, JSON-object validation, key/value string conversion, and exception behavior. It passes the unescaped bytes to `json.loads`, whose UTF-8 decoding is equivalent for the JSON emitted by `osmium` and avoids a separate Python-level decode allocation.

### Tag extraction

`_localized_values` retains exact suffix matching, excludes the empty suffix, excludes whitespace-only values, and returns keys in sorted order. It gathers only matching `(suffix, value)` pairs and sorts that smaller collection. Records without localized keys therefore avoid sorting their complete tag dictionaries while preserving deterministic output for records that do contain localized values.

### Data flow and invariants

The data flow remains:

```text
osmium COPY line
  → split fields and decode escapes
  → ExportRecord with complete tags
  → description-first rejection check
  → geometry/area transformation for included rows
  → atomic GeoParquet write and validation
```

The following invariants are unchanged:

- Every original OSM tag is preserved.
- Description and name extraction preserves exact values and suffixes.
- Rejection reasons and counts remain stable.
- Geometry, area, bounding boxes, timestamps, schema, ordering, and output identities remain stable.
- Invalid COPY fields, invalid UTF-8, malformed JSON, unknown escapes, and trailing backslashes retain their existing failure or decoding behavior.
- Streaming, bounded memory, atomic output promotion, manifest generation, resumability, and publication boundaries remain unchanged.

## Verification design

The implementation follows RED → GREEN:

1. Add focused tests for the no-escape COPY path, escaped COPY values, bytes-native tag parsing, and localized-value ordering/filters; run them to observe the expected pre-optimization failure where the new fast-path contract is asserted.
2. Implement the smallest changes described above.
3. Run the focused extraction and transform tests, then the complete regression suite.
4. Rebuild the same Afghanistan PBF into a fresh isolated temporary root using the baseline command.

The real-data comparison must show:

- identical included/emitted counts and rejection counts;
- identical Parquet schema, row values, row order, and Parquet SHA-256;
- unchanged source/output identities, schema/transform/area-policy revisions, dependency versions, counts, and rejection data in the manifest; `code_revision` and run timestamps are expected to identify the new build;
- no new files in the repository or immutable/generated data roots;
- lower end-to-end wall time than 434.84 seconds on the same host and environment. A reduction of at least 20% is the acceptance target; otherwise the optimization is not retained.

The repository quality gate remains the final check: `just check`, with its package build retried only with network access if the sandbox cannot resolve PyPI. The focused real-data benchmark is supplementary and never contacts Hugging Face or publishes artifacts.

## Rollback boundary

If any semantic comparison, regression test, output identity, or performance acceptance check fails, retain the baseline implementation and report the failed gate. The change is isolated to parser/transform internals, so rollback is limited to the two production files and their focused tests.
