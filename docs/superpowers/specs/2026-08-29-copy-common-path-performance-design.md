# COPY Common-Path Performance Design

**Date:** 2026-08-29

## Goal

Reduce the remaining Python-side cost of the PBF-to-GeoParquet build after the
first parser optimization, without changing dataset bytes, schemas, workflow
contracts, error behavior, or resource boundaries.

The current profile of the 107 MB Afghanistan source records 2,147,728 polygon
rows. JSON tag decoding accounts for 30.7 seconds of cumulative Python time,
scalar COPY decoding accounts for 10.5 seconds, and the normal record path
allocates for a whitespace check on every line. Representative tag payloads
decode 6–11× faster with `orjson` than with the standard-library decoder.

## Scope

- Declare `orjson` as a direct runtime dependency. It is already present in the
  locked environment through Trackio, so this makes the import explicit rather
  than relying on a transitive dependency.
- Decode normal UTF-8 JSON tag payloads with `orjson` from bytes. If the fast
  decoder rejects a payload that the standard library accepts, fall back to
  `json.loads` so the existing permissive parser behavior remains available.
- Skip COPY unescape calls for scalar fields without backslashes. Use the
  existing state machine for escaped and non-ASCII edge cases.
- Check the first byte before calling `strip()` in `iter_records`; ordinary
  osmium records never begin with whitespace, while whitespace-only lines keep
  their current skip behavior.

No concurrency, subprocess command, output format, schema, ordering, storage,
publication, or data-root behavior changes.

## Invariants

- Escaped values, unknown escapes, trailing backslashes, null fields, malformed
  JSON, non-ASCII numeric text, and whitespace-only lines retain their current
  behavior.
- The `ExportRecord` contract and all rejection counts remain unchanged.
- `orjson` is used only as an acceleration for valid UTF-8 JSON. The standard
  library remains the compatibility fallback for accepted extensions and
  decoder edge cases.
- The same source and dependency environment must produce a byte-identical
  Parquet artifact and matching normalized manifest metadata.

## Verification

1. Add tests that fail before the optimization because the current parser calls
   the standard decoder, unescapes every plain field, and strips every normal
   record line.
2. Implement the smallest common-path changes and run focused tests.
3. Run formatting, linting, typing, the complete quality gate, and package build.
4. Rebuild the Afghanistan source in a fresh temporary root and compare counts,
   normalized manifest fields, and Parquet SHA-256 against the existing
   105.67-second baseline artifact.
5. Retain the change only if the optimized build is faster and all semantic
   comparisons pass.

## Rollback boundary

Rollback is limited to the direct dependency declaration, the COPY extraction
module, and its focused tests. No raw source or generated project data is
modified.
