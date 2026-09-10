# Language Detection V2 Design

> Historical design note: this document describes the superseded Lingua-only V2 pilot. The active pipeline uses Lingua 2.2.0 as primary and GlotLID v3 only as a fallback when Lingua is uncertain.

## Goal

Run a directly comparable second language-detection pilot for Afghanistan,
Albania, and Algeria, with higher usable-label coverage while preserving the
existing conservative handling of short and mixed-language descriptions.

## Scope

V2 is a separate immutable run. It uses the same source snapshot contents and
the pinned `lingua-language-detector==2.2.0` implementation as V1. Alsace is
excluded from the V2 execution because it is a region rather than a country.

The only detection-policy change is lowering `min_score` from `0.80` to
`0.70`; `min_margin` remains `0.20`, `min_alphabetic_chars` remains `5`, and
mixed-language and short-text results remain uncertain. V1 artifacts are never
rewritten.

## Architecture

The existing `LanguagePolicy` remains the single policy object. A named V2
factory/constant provides the approved settings so the run metadata records a
distinct configuration fingerprint. The existing snapshot, checkpoint,
annotation, validation, and Grid'5000 workflows are reused unchanged.

V2 output is written under a new run directory on the Seagate volume. Each
country shard is prepared, transferred, submitted, monitored, retrieved, and
validated independently, with one core and a maximum 30-minute walltime. Every
submission collects fresh usage-policy, quota, and active-job evidence and
fails closed when evidence is unavailable or the account already has an active
job.

## Comparison and decision

After all three shards complete, a read-only comparison reports:

- description-entry coverage and V1-to-V2 status changes;
- per-country detected/uncertain counts and language distributions;
- raw score and margin distributions;
- mixed-text and short-text counts;
- duplicate and validation errors; and
- a deterministic sample of changed or uncertain descriptions for manual
  review.

V2 may be recommended for broader processing only if it improves coverage
without introducing validation defects or obvious language errors. Afghanistan
is reported separately because Lingua does not provide Pashto or Dari language
models; Persian labels must not be treated as proof that Pashto or Dari was
identified correctly. No Hugging Face publication is performed for this pilot.

## Testing

Tests cover the named V2 policy values, its distinct model-configuration
fingerprint, and preservation of the existing default policy. Focused tests
run first through the red-green-refactor cycle, followed by the project’s
quality gates before commit and push.
