# Repository hardening and language release design

**Date:** 2026-09-19

**Status:** Approved for implementation

## Goal

Leave the repository maintainable, tested, documented, and free of unresolved repository issues, then recompute the language annotations with the approved ungated-confidence policy across all 386 source shards and publish a verified replacement `language-v1` export to Hugging Face.

## Scope

The work covers the six currently open GitHub items: language policy publication (#21), the all-source mutation gate (#20), Grid'5000 operator cohesion (#8), language CLI cohesion (#9), repeated test setup (#10), and private publication re-exports (#11). It also covers the associated documentation, generated artifact hygiene, GitHub review/merge state, Grid'5000 execution, and Hugging Face publication.

Raw source data and unrelated datasets remain read-only. Existing public artifacts are replaced only after the new run is complete, validated, and independently verified.

## Design

### Stable behavior boundaries

The language behavior change is limited to the already-approved policy change:

- empty or whitespace-only values and values with no letters remain `non_linguistic`;
- values with fewer than five alphabetic characters remain `uncertain`;
- exact top-score ties and mixed-language evidence remain `uncertain`;
- otherwise the best model label is accepted without minimum score or margin gates;
- Lingua remains primary, GlotLID remains fallback-only, and sentence splitting remains gated by detected and SaT-supported languages.

Raw text, annotation cardinality, score columns, model revisions, checksum validation, checkpoint semantics, and CLI flags unrelated to the removed confidence gates remain unchanged.

### Modular refactor

The refactor is a move-and-contract operation, not a rewrite:

- `workflow/grid_operator.py` becomes a package with focused modules for scalar validation, submission intent/state validation, bundles and staging, job scripts/payloads, and submission/resumption. The package initializer re-exports the established public names.
- `dataset/languages/language_cli.py` retains argument parsing, dispatch, and output formatting. Workflow decisions move to testable workflow modules and retain the existing CLI surface and exit behavior.
- The publication package exposes only supported public names. Helpers required as contracts receive explicit public names and docstrings; genuinely internal helpers remain in their defining modules.
- Repeated test setup is promoted into package-local fixtures. Temporary directories use pytest-managed paths. Test names and collected test count are preserved.

Every boundary gets direct tests before production refactoring. Compatibility tests protect imports, CLI output, serialized state, and release identities.

### Mutation quality

The mutation gate remains a 100% requirement. Surviving mutants are exported and classified. Real behavioral gaps receive focused tests; equivalent mutants are eliminated through the smallest explicit implementation or test-contract improvement. No baseline ratchet or silent exclusion is introduced.

### Release protocol

The release uses the existing resumable Grid'5000 driver and its durable checkpoint/reconciliation model:

1. freeze the source snapshot and compute the new configuration fingerprint;
2. validate Grid'5000 access, quota, model availability, and run identity;
3. process all 386 shards with no duplicate submission after ambiguous scheduler state;
4. validate every shard, manifest, count, identity, and conservation invariant;
5. publish the complete language export to `NoeFlandre/osm-polygon-description-tag`;
6. verify remote file inventory, hashes, metadata, and statistics independently;
7. rerun the publication command and require a zero-mutation no-op.

The previous conservative export is never treated as evidence for the new fingerprint.

## Quality gates

Before any external mutation, the branch must pass:

- lockfile and pre-commit validation;
- Ruff formatting and linting;
- `ty` type checking;
- the complete pytest suite with coverage threshold;
- CRAP budget validation;
- the full mutation gate with zero survivors and zero unresolved statuses;
- strict documentation build;
- package build and CLI smoke checks;
- focused Grid/release contract and integration tests;
- clean diff and clean worktree review.

## GitHub completion

The implementation is pushed through a reviewable feature branch. The open language PR is updated or superseded, CI and review findings are addressed, and the branch is merged only after all gates pass. Issues #8, #9, #10, #11, #20, and #21 are closed with links to the relevant commits, test evidence, and release verification. GitHub state is re-read after merge to confirm no unresolved issue or stale open PR remains.

## Out of scope

No unrelated project redesign, source-data regeneration, deletion of raw data, weakening of quality gates, or removal of tests solely to improve metrics is allowed.
