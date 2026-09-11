# Language rollout status

A living record of the `language-v1` cascade rollout: what is finished, what is
not, and what the next operator has to do. Update it in the same commit as the
work it describes.

**Last updated:** 2026-09-11 · **Code:** `8508b8a` on `main`

## Summary

| Stage | State |
| --- | --- |
| Cascade implementation (Lingua primary, GlotLID v3 fallback) | **Done** |
| Local quality gates except mutation | **Done** |
| Mutation gate at 100 % | **Not done** — 94.63 %, 921 survivors |
| Grid'5000 full-dataset run | **Not done** — prepared, blocked |
| Hugging Face publication | **Not done** — blocked on the run above |

## Done

### Detector cascade

Lingua 2.2.0 stays primary and keeps the conservative policy. The pinned
GlotLID v3 model runs only when Lingua returns `uncertain`; a GlotLID result
that is not `detected` leaves the Lingua result untouched. Fallback-resolved
rows carry `reason=fallback_glotlid_v3`. The snapshot records the exact GlotLID
repository, revision, runtime and model SHA-256, so a run cannot silently
switch models. See [the runbook](language-detection.md) for the contract.

### Contracts pinned against mutation

Provenance- and safety-bearing values are asserted exactly rather than
spot-checked, because mutation testing showed loose assertions let real defects
through:

- cascade and GlotLID configuration fingerprints, by exact digest;
- both rsync argv, element by element, including trailing-separator and root
  normalisation;
- remote-path validator messages in full, so each names the input it refused;
- `submit_job`'s fail-safe defaults: no implicit apply, `night=noretry`, no
  unrequested freshness demand, and the caller's walltime and retry choice
  carried into both the plan and the submitted command.

### Mutation gate correctness

A function with no recorded trampoline hit used to inherit only its sibling
functions' tests, which reported killable mutants as survivors. Such a function
now runs the whole collected suite. This affected 221 of 1 096 functions and
409 of 998 unresolved mutants.

### Quality gates

2 610 passed / 1 skipped, 99.23 % branch coverage, CRAP max 5.474, Radon max
complexity 5, ruff format and lint, `ty`, pre-commit, `uv lock --check`,
`uv build`, wheel contents, strict MkDocs.

## Not done

### 1. Mutation gate at 100 %

Current candidate: **16 764 / 17 716 killed (94.63 %)** — 921 survived,
30 timeout, 1 interrupted. Reproduce with
`python scripts/check_mutation_score.py --mutants-root mutants --output
reports/mutation-summary.json --minimum-score 100`.

The denominator is short of the full 18 054: `validation.py`'s 338 mutants were
reset when an interrupted run truncated its `.py.meta`, and mutmut has not yet
regenerated them. Expect the total to return to 18 054 on the next full run.

Mechanics already verified, so nobody needs to re-diagnose them:

- The escalation ladder is sound and cheap. For every cluster checked, the
  killing test is inside the stage-two selection of 40 tests, so killable
  mutants do not pay for the full suite.
- `mutmut._run` **does** re-execute a mutant that already has a cached exit
  code, so a resumed run picks up newly added tests. This was confirmed
  directly: `models.x__cascade_fingerprint__mutmut_1` went from exit code 0
  (survived) to 1 (killed) on a re-run with no source change.
- The runtime is dominated by *true* survivors. A function with no recorded
  trampoline hit runs the whole 2 618-test suite in the final stage, so each
  surviving mutant costs minutes. With ~900 survivors a full convergence run is
  several hours, and the candidate lives on the external drive, which makes it
  slower still.

Next steps:

1. Run the gate to completion on an idle machine. Do not interrupt it: killing
   it mid-write has twice truncated a `.py.meta`, which silently drops that
   file's results. If it must be stopped, check for zero-byte `.py.meta` files
   afterwards and delete them so mutmut regenerates them.
2. Work the clusters. The recurring shapes are unasserted error-label strings,
   unasserted keyword-argument defaults, and payload or argv content that no
   test compares exactly. Pinning one such contract typically kills a whole
   cluster: six functions and 121 mutants fell to four tests this way.
3. Prefer test-only fixes: they do not change `fingerprint_project_source`,
   so they do not invalidate a prepared Grid snapshot. Remove genuinely
   unreachable code rather than excluding its mutants.

### 2. Grid'5000 full-dataset run

Prepared and verified, not submitted.

- Snapshot `639783b081d7caa7e6cc6e6db501e81768de821df448a6596aa6aff4daaa5088`
  is frozen over all 386 shards (906 631 rows) under the v1 policy, in
  `data-root/language-run-lingua-glotlid-v3-full`.
- The pinned GlotLID artifact is staged at
  `nancy:/home/nflandre/models/glotlid-v3/model_v3.bin`; its SHA-256 matches
  the pinned constant.
- One shard's portable payload transferred to
  `nancy:/home/nflandre/osm-language-grid-v3/`, with all 106 staged files
  byte-identical to `stage.json`.

Two blockers:

1. **Frontend operator environment is broken.** Every `grid` subcommand fails
   before reaching the scheduler with `NumPy was built with baseline
   optimizations: (X86_V2) but your machine doesn't support: (X86_V2)`. Rebuild
   it for a CPU baseline the frontend satisfies and confirm with
   `osm-polygon-description-tag --help`.
2. **Policy window.** Weekday daytime in Europe/Paris is refused by default,
   and the whole walltime must fit one side of the 09:00/19:00 boundary, so
   work happens at night unless the operator has confirmed their own daytime
   accounting and passes `--allow-daytime`.

At roughly three minutes per shard end to end and a concurrency of one, a full
pass is on the order of twenty hours of supervised submission. The three
completed shards of the earlier V2 pilot are **not** reusable: `snapshot_id`
binds the code and lockfile fingerprints, and both have changed.

### 3. Hugging Face publication

Blocked by design, not skipped. `export` refuses any run that is not complete
(currently 3 of 386 shards under the old pilot snapshot, 0 of 386 under the new
one), so nothing can be published until stage 2 finishes. The dataset repo is
reachable and authenticated; it currently carries **no** `language-v1/` files.

## Performance notes

Profiled before changing anything, which argued against changing anything:

| Measurement | Value |
| --- | --- |
| Warm throughput | ~307 rows/s, ~320 detections/s |
| `detect_multiple_languages_of` | 66.5 % of warm time |
| `compute_language_confidence_values` | 23.4 % |
| Our Python wrapper | ~10 % |
| Cold start (Lingua's 293 MB of models) | 57–95 s per process |
| Description extraction | 0.8 % |

Three candidate optimisations were measured and rejected: whitespace-normalised
cache keys (0.17 % of calls), a larger LRU (4 096 entries wastes 321
re-detections ≈ 1 s across the six largest shards), and Arrow-level tag pruning
(extraction is 0.8 %). The remaining in-repo cost, double validation of
confidence values (~8.5 %), is load-bearing for the public contract of
`compute_language_confidence_values`.

The dominant structural cost is cold start: whole-dataset inference is roughly
49 minutes warm, while 386 one-shard jobs pay about 6.4 hours of repeated model
loading. Amortising that would mean processing several shards per job, which
trades away the one-shard-per-job property.
