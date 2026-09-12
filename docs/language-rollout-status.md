# Language rollout status

A living record of the `language-v1` cascade rollout: what is finished, what is
not, and what the next operator has to do. Update it in the same commit as the
work it describes.

**Last updated:** 2026-09-12 · **Code:** `main`

## Summary

| Stage | State |
| --- | --- |
| Cascade implementation (Lingua primary, GlotLID v3 fallback) | **Done** |
| Local quality gates | **Done** |
| Mutation gate at 100 % | **Done** — 17 726 / 17 726 killed |
| Grid'5000 operator environment | **Done** — the NumPy baseline blocker is resolved |
| Grid'5000 full-dataset run | **Not done** — unblocked, not yet executed |
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

### Grid'5000 operator environment

The frontend blocker is resolved and the cause is recorded so nobody
re-diagnoses it. `fnancy` presents a `Common KVM processor` whose
`/proc/cpuinfo` flags include `pni` and `cx16` but neither `sse4_2` nor
`popcnt`: an x86-64 baseline machine with SSE3. NumPy 2 wheels target
x86-64-v2 and abort on import, which is what broke every `grid` subcommand
before it reached the scheduler. NumPy 1.26 wheels have an SSE3 baseline and
run there.

Pinning only the *operator* environment to `numpy==1.26.4` is therefore
sufficient and changes no model semantics: the generated job script runs
`uv sync --frozen --no-dev --extra language` on the allocated compute node, so
inference always uses the locked environment. Confirmed on 2026-09-11 by
running `language grid status` on the frontend through that environment: it
reaches the snapshot identity check and reports a fingerprint mismatch --- the
stale operator copy of the project --- instead of failing on import.

Refresh the operator copy from the committed tree before a run, or the
fingerprint guard will keep refusing.

### Quality gates

3 172 passed / 1 skipped, 99.50 % branch coverage, CRAP max 5.022, Radon max
complexity 5, ruff format and lint, `ty`, pre-commit, `uv lock --check`,
`uv build`, wheel contents, strict MkDocs.

The single skip is the Docker smoke test, which needs `RUN_DOCKER_SMOKE=1`.
Run the suite with a `TMPDIR` free of shell metacharacters or two end-to-end
job-script tests skip as well; see the note at the end of this document.

### Mutation gate at 100 %

`python scripts/check_mutation_score.py --mutants-root mutants --output
reports/mutation-summary.json --minimum-score 100` reports **100.00 %
(17 726 / 17 726)** with every unresolved bucket at zero: no survivor, timeout,
`no_tests`, skipped, suspicious, segfault, or interrupted mutant.

Reaching it from 624 survivors took three kinds of change, in this order of
preference.

**Tests, wherever a mutant was observable.** The recurring shapes were
unasserted refusal wording, argument forwarding that a permissive fake
swallowed, payload and argv content nothing compared by value, and boundary
values tested from one side only. Two habits did most of the work: assert the
whole refusal with `exactly()`, and make a fake *record* its arguments instead
of accepting any. A fake declared as `lambda *_: value` proves only that a call
happened.

**Source simplifications, wherever the mutant was equivalent because the code
was redundant.** Each removed the mutant by removing the redundancy, and each
is an improvement on its own terms:

- `shards_root`, `jobs_root` and `_project_source_root` give the three owned
  directory names one definition each, pinned by value in one test. A
  filesystem assertion cannot pin a path's spelling on a case-insensitive
  volume, which is why `shards` versus `SHARDS` survived for so long.
- `_verify_derived_fields` compared six payload fields with the values it had
  just handed the constructor; only the four `init=False` fields can disagree.
- `process_shard` built a checkpoint it never used on the only path that
  reached the call.
- `_run_directory_exists` returned a bool nobody read; it is now a
  `_require_regular_run_directory` validator.
- `_validate_remote_path` rejected a `.` component that `PurePath` had already
  dropped while parsing.
- `_run_wide_intent_block` skipped this job's own intent file twice: a job
  directory is named after its bundle, and `_iter_intents` already refuses an
  intent that does not bind to its directory's bundle.
- `prepare_job` validated the same three limits twice, once through
  `render_job_script` and once through `_job_config_payload`.
- The three callers that validate an annotation table without reserving its
  identities now say so by name, through
  `validate_annotation_table_without_reserving`, rather than by passing
  `merge_seen=False`.
- `quarantine_orphan_artifacts` takes `_both_state_locks` directly instead of
  passing a constant `submission_locked=False`.

**Pragmas, only with a written line-specific proof.** Six were added, listed
below with the others.

#### Two traps worth recording

`ruff format` moves a trailing comment onto a closing bracket, where a
line-scoped pragma no longer annotates the statement it was written for. Worse,
mutmut only honours a trailing pragma on a *single-line* statement: it records
the line the statement *starts* on, so a pragma written against one argument of
a multi-line call is silently ignored. Three pragmas added during this work did
nothing for that reason and were replaced by the source changes above. Check a
new pragma by confirming the mutant it targets stops being generated.

The second trap is the gate's own test selection. Per-test coverage contexts
override mutmut's recorded association, so a *stale* context file hides newly
added tests from selection and they never run: the mutants they kill keep
reporting as survivors. Re-record `just mutation-contexts` after adding tests,
and delete `mutants/mutmut-stats.json` and `mutants/mutmut-recorded-tests.json`
when test ids change --- a renamed parametrisation otherwise aborts the run's
clean preflight with `not found:`.

#### Equivalence classes that are excluded rather than killed

Each is visible in source and carries a line-specific proof. A pragma is
line-scoped, so it also suppresses every other mutation of that line; where that
cost was real the redundancy was removed instead, as listed above.

- `# pragma: no mutate - static cast`: `typing.cast` erases its type argument
  at runtime, so `cast(None, x)` cannot change behaviour.
- `# pragma: no mutate - codec alias only`: codec names are case-insensitive,
  so `"UTF-8"` and `"utf-8"` resolve to the same codec.
- `# pragma: no mutate - digest names are case-insensitive`: `hashlib` resolves
  `"SHA256"` and `"sha256"` to the same algorithm.
- `# pragma: no mutate - newline domain is LF or CRLF`: the value comes from
  the front-matter regex `\\r?\\n`, so the non-LF branch is the only remaining
  supported alternative.
- `# pragma: no mutate - distinct find offsets`: the two distinct markers
  independently return `-1` or their non-negative position, so the combined
  boundary check cannot be distinguished by changing one comparison.
- `# pragma: no mutate - rfind starts at zero`: omitting `str.rfind`'s start
  argument is equivalent to passing `0` for this line-prefix lookup.
- `# pragma: no mutate - same falsy default`: a missing or null Hub SHA is
  normalized by `or ""`, so `getattr` defaults of `""` and `None` are identical.
- `# pragma: no mutate - no upper-case X can occur`: the stem is lower-cased
  before it is stripped, so stripping `"-"` and stripping `{"X", "-"}` remove
  the same characters.
- `# pragma: no mutate - reassigned before its first read`: the two row-group
  alignment variables are assigned on the loop's first iteration, and with no
  row groups the loop never runs and the early return reads neither.
- `# pragma: no mutate - equal or refused either way`: the remote bundle root is
  taken from one of three siblings whose parents must already agree, and the
  check below refuses whichever parent was chosen when they do not.
- `# pragma: no mutate - any fixed offset`: `astimezone` makes the offset fixed
  so the addition is absolute rather than wall-clock; every fixed-offset zone
  gives the same instant, and the result is only read through
  `is_weekday_daytime`.
- Two `# pragma: no mutate start` / `end` blocks cover a provisional
  `identity_sha256` that `UploadPlan.to_payload` omits by design, and a dry-run
  delegation whose `apply` argument is already falsy on that path.

#### The gate is selection-driven, not guesswork

The cost used to be the gate's own test selection. Mutmut associates a function
with the tests that entered its trampoline, and 221 of 1 096 functions had no
recorded test at all, which forced the gate to run the whole 2 618-test suite
for each of their mutants.

Per-test coverage contexts give the same association exactly. Mutmut mutates
function bodies, so a test that never executes a line of a function cannot
observe that function's mutation: the covering tests are precisely the tests
that can kill its mutants. `just mutation` now records that map first
(`just mutation-contexts`) and the gate consumes it.

Measured effect on a full sweep:

| | Before | After |
| --- | --- | --- |
| Test executions | 748 103 | 149 525 (**5.0x fewer**) |
| Selection per function, median | 161 | 79 |
| Selection per function, mean | 683 | 136 |
| Worst selection | 2 618 | 737 |
| `_cascade_fingerprint` | 2 618 | 73 |
| `glotlid._score` | 2 618 | 38 |

The map is keyed by mutmut's own mangled names, derived from the AST; all 1 096
names it generates are covered, and a contract test pins that shape so the fast
path cannot degrade silently. Where coverage says nothing about a function, the
previous selection stands, because running more tests is always sound.

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

#### Running it, and keeping the run honest

Do not interrupt the gate: killing it mid-write has twice truncated a
`.py.meta`, which silently drops that file's results. If it must be stopped,
check for zero-byte `.py.meta` files afterwards and delete them so mutmut
regenerates them. Convergence from 624 survivors took eight passes on an SSD
scratch copy, and the sequence that finally worked is worth repeating verbatim:

1. run the suite, then re-record `just mutation-contexts`;
2. delete `mutants/mutmut-stats.json` and `mutants/mutmut-recorded-tests.json`;
3. run the gate;
4. confirm with `scripts/check_mutation_score.py --minimum-score 100`.

Give the gate a `TMPDIR` nothing else uses. Sharing one with an ad-hoc `pytest`
run deletes the numbered directory underneath it and its stats collection dies
with `FileNotFoundError`.

Mutmut re-executes a mutant that already has a cached exit code, so a resumed
run does pick up newly added tests. This was confirmed directly:
`models.x__cascade_fingerprint__mutmut_1` went from exit code 0 (survived) to 1
(killed) on a re-run with no source change.

Source changes here invalidate a prepared Grid snapshot, because `snapshot_id`
binds `fingerprint_project_source`. This work changed `src/`, so the snapshot
must be re-frozen before the run below; that is already listed as step 1 there.

## Not done

### 1. Grid'5000 full-dataset run

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

The frontend blocker is resolved; see the operator-environment section above.
What remains is scheduling, plus two steps that must happen in this order.

1. **Re-freeze the snapshot.** `snapshot_id` binds `fingerprint_project_source`
   over `src/`, and this work changed `src/`. Snapshot
   `639783b0...` is therefore stale, and so is the one staged shard on `nancy`.
   Re-run `language prepare` against the current tree before staging anything.
2. **Refresh the operator copy on the site.** The editable environment under
   `~/osm-language-grid/project` is a different vintage of the code, so
   `grid status` refuses with `model identity field does not match derived
   value: config_fingerprint`. Sync the committed tree there first.

**Policy window.** The account owner has authorised this rollout's weekday
daytime accounting, so the operator may pass `--allow-daytime`; without that
flag, daytime remains refused by default. The whole walltime must still fit one
side of the 09:00/19:00 Europe/Paris boundary, and every other fail-closed
policy check remains mandatory. The weekend is not weekday daytime and is
therefore unrestricted.

At roughly three minutes per shard end to end and a concurrency of one, a full
pass is on the order of twenty hours of supervised submission — more than one
night window, so plan for a weekend or several nights. The three completed
shards of the earlier V2 pilot are **not** reusable: `snapshot_id` binds the
code and lockfile fingerprints, and both have changed.

`scripts/run_language_grid.py` sequences the per-shard protocol and is
resumable; it halts rather than resubmitting anything ambiguous. See
[the runbook](language-detection.md) for how to invoke it and why the transfer
has to be arranged over SSH rather than by `grid stage --apply`.

### 2. Hugging Face publication

Blocked by design, not skipped. `export` refuses any run that is not complete
(currently 3 of 386 shards under the old pilot snapshot, 0 of 386 under the new
one), so nothing can be published until stage 1 finishes. The dataset repo is
reachable and authenticated; it currently carries **no** `language-v1/` files.

## Running the suite from the mounted volume

Keep temporary directories **outside** the project tree: several tests walk up
from a temp path looking for `pyproject.toml`, and a `TMPDIR` inside the repo
makes them find the real one. `/Volumes/Seagate M3/tmp/osm-pdt` works.

Two end-to-end job-script tests skip when the temp path contains a shell
metacharacter, which every absolute path on a volume named `Seagate M3` does.
That is not a defect: a remote path may not contain shell metacharacters,
because OAR evaluates the stored command through a shell. Point `TMPDIR` at a
plain path to run them.

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
