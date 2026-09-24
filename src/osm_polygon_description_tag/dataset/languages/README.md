# Languages package

## Purpose

Annotate every actual OpenStreetMap description value with deterministic,
resumable processing. Confidence is not gated: scores are recorded, not thresholded. Lingua 2.2 is primary; the pinned GlotLID v3
model is consulted only when Lingua returns `uncertain`.

## Responsibilities

Own the immutable input snapshot, the pinned detector adapter and its
structural policy, the per-description record contracts, the frozen
annotation schema, durable checkpoints and receipts, the bounded streaming
worker, and read-only completeness validation.

## Non-responsibilities

This package does not orchestrate Grid'5000, publish to the Hugging Face Hub,
render terminal output, or modify the original dataset, its GeoParquet files,
or schema 3.

## Public API

Import stable contracts from `osm_polygon_description_tag.dataset.languages`,
for example `LanguagePolicy`, `LanguageResult`, `DescriptionEntry`,
`build_lingua_detector`, `build_language_detector`, and
`extract_description_entries`. Snapshot,
checkpoint, worker, annotation, and validation modules are imported directly.

## Allowed dependencies

The `runtime` package, other `dataset` modules, PyArrow, and Python's
standard library. Lingua and the Linux-only GlotLID runtime are optional extras
imported lazily, never at module load.

## Data flow and side effects

`prepare_snapshot` freezes source identity; `process_shard` streams projected
Arrow batches from one staged Parquet and commits each batch as an annotation
part, a receipt, and a checkpoint, in that order. `validate_run` only reads.

## Safety and determinism invariants

The counting unit is one description value, not one polygon. Original text is
preserved exactly and localized suffixes are opaque. A fallback
result is marked with reason `fallback_glotlid_v3`; unresolved fallback output
keeps Lingua's original `uncertain` result. Scores are raw detector
outputs, never calibrated probabilities, and no accuracy is claimed. The
checkpoint is the single source of truth on resume, so an interruption at any
write boundary neither loses nor duplicates annotations. Every persisted
payload is validated rather than coerced.

## Tests

Run `uv run pytest tests/unit/dataset/languages -q`.
