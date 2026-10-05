# Language rollout status

This page is a living record of the `language-v1` cascade rollout. It shows what
is finished, what is not finished, and what the next operator must do. Update
this page in the same commit as the work that it describes.

**Last updated:** 2026-09-23 · **Code:** `main`

## Summary

| Stage | State |
| --- | --- |
| Cascade implementation (Lingua primary, GlotLID v3 fallback) | **Done** |
| Sentence splitting (SaT-3l-sm, gated on the languages it was trained on) | **Done** |
| Local quality gates | **Done** |
| Mutation gate at 100 % | **Not met**. 20 708 / 20 775 killed (99.677 %), 67 survivors. Refer to [#20](https://github.com/NoeFlandre/osm-polygon-description-tag/issues/20) |
| Grid'5000 operator environment | **Done**. The NumPy baseline blocker is resolved |
| Grid'5000 full-dataset run (ungated policy) | **Done**. 386 / 386 shards, 906 631 rows, 919 126 annotations |
| Hugging Face publication (ungated policy) | **Done**. Revision `d144ca6a`, 388 files under `language-v1/` |

## Done

### Sentence splitting

The tool splits the descriptions into sentences in the **same pass** as the
detection. The cold start is the main cost of a Grid run. 386 one-shard jobs pay
about 6.4 hours of repeated model loading. The warm inference takes about 49
minutes. A second sweep would almost double the cost. It would compute again
something that is already in memory. The splitting is a pure function of the
text and the detection result.

The tool splits a description only when the detection settled on a language
*and* the team trained the splitter on that language. SaT-3l-sm takes no
language at inference. It is language-agnostic. In `wtpsplit`, `lang_code`
selects a style adapter that this configuration does not use. The 85 languages
of its supervised mixture are the languages where it is *known to be competent*.
That list is the gate. The tool publishes everything else unsplit and records
the reason. The reason is `unsupported_language_<iso 639-3>` when the language
is known but outside the 85 languages. The reason is `not_detected_<status>` when
the detection did not settle. Croatian is the clearest case. Lingua detects it
with confidence, and the team never trained SaT on it.

The source pins the supported set. The tool does not read it from the installed
library. The tool puts the set and the pinned artifact into
`model_config_fingerprint`. Thus, if you change one of them, the run identity
changes. The dataset does not change without a message. The detection reports
ISO 639-3. SaT names its languages in ISO 639-1. An explicit table joins the
two. The table also covers the macrolanguage members that a pinned detector
emits (`nob`/`nno` to `no`, `arb` to `ar`, `cmn` to `zh`). The tool treats an
unmapped code as unsupported. It guesses nothing. Refer to
[the runbook](language-detection.md#sentence-splitting).

Two facts about `wtpsplit` are important. Both would fail only after a job
already runs on a node:

- `SaT` loads a **directory**, in the same way as `transformers`. It does not
  load a weights file. Thus `--sat-model-path` is a directory that has
  `config.json` next to `model.safetensors`. The tool checks the pinned SHA-256
  on the weights inside it.
- Its tokenizer argument fetches `xlm-roberta-base` from the Hub by default. A
  compute node has no reason to have network access. Thus the tokenizer is
  staged into that same directory and the tool names it explicitly.

### Detector cascade

Lingua 2.2.0 stays primary. The cascade does not gate the confidence. Only the
structural checks (length, ties, mixed text) make a result `uncertain`. The tool
runs the pinned GlotLID v3 model only when Lingua returns `uncertain`. If a
GlotLID result is not `detected`, the Lingua result stays unchanged. The rows
that the fallback resolves have `reason=fallback_glotlid_v3`. The snapshot
records the exact GlotLID repository, revision, runtime, and model SHA-256. Thus
a run cannot change the model without a message. Refer to
[the runbook](language-detection.md) for the contract.

### Contracts pinned against mutation

The tests assert the provenance values and the safety values exactly. They do
not spot-check them. The mutation testing showed that loose assertions let real
defects pass:

- the cascade and GlotLID configuration fingerprints, by exact digest;
- both rsync argv, element by element, including the trailing-separator and root
  normalisation;
- the full messages of the remote-path validator, so that each names the input
  that it refused;
- the fail-safe defaults of `submit_job`: no implicit apply, `night=noretry`, no
  unrequested freshness demand, and the walltime and retry choice of the caller
  in both the plan and the submitted command.

### Mutation gate correctness

A function with no recorded trampoline hit used to inherit only the tests of its
sibling functions. This reported killable mutants as survivors. Now such a
function runs the whole collected suite. This affected 221 of 1 096 functions
and 409 of 998 unresolved mutants.

### Grid'5000 operator environment

The frontend blocker is resolved. This page records the cause so that nobody
needs to diagnose it again. `fnancy` presents a `Common KVM processor`. The
`/proc/cpuinfo` flags include `pni` and `cx16`. They do not include `sse4_2` or
`popcnt`. Thus it is an x86-64 baseline machine with SSE3. The NumPy 2 wheels
target x86-64-v2 and stop on import. This broke each `grid` subcommand before it
reached the scheduler. The NumPy 1.26 wheels have an SSE3 baseline and run
there.

It is enough to pin only the *operator* environment to `numpy==1.26.4`. This
does not change the model semantics. The generated job script runs `uv sync
--frozen --no-dev --extra language` on the allocated compute node. Thus the
inference always uses the locked environment. The team confirmed this on
2026-09-11. It ran `language grid status` on the frontend through that
environment. The command reaches the snapshot identity check. It reports a
fingerprint mismatch (the operator copy of the project is stale). It does not
fail on import.

Before a run, refresh the operator copy from the committed tree. If you do not,
the fingerprint guard continues to refuse.

### Quality gates

The results are: 3 336 passed / 3 skipped, 99.49 % branch coverage, ruff format
and lint, `ty`, pre-commit, `uv lock --check`, `uv build`, wheel contents, and
strict MkDocs.

One skip is the Docker smoke test. It needs `RUN_DOCKER_SMOKE=1`. The other two
skips are the end-to-end job-script tests. They skip when `TMPDIR` contains a
shell metacharacter. A path that contains a space does this. This is the guard
at work. It is not a gap. A remote path cannot contain metacharacters, because
OAR evaluates the stored command through a shell. To run these tests, point
`TMPDIR` at a plain path. Refer to the note at the end of this document.

### Mutation gate at 100 %

`python scripts/check_mutation_score.py --mutants-root mutants --output
reports/mutation-summary.json --minimum-score 100` reports **100.00 %
(20 773 / 20 773)**. Each unresolved bucket is zero: no survivor, timeout,
`no_tests`, skipped, suspicious, segfault, or interrupted mutant. This is the sum
of the eight shards of the all-source gate. Each shard requires 100 % on its
own. The figure above is from run `35526775857`.

The team went from 624 survivors to this result with three kinds of change. The
list shows them in the order of preference.

**Tests, where a mutant was observable.** The shapes that occurred again and
again were: refusal wording without an assertion, argument forwarding that a
permissive fake swallowed, payload and argv content that no test compared by
value, and boundary values that a test checked from one side only. Two habits
did most of the work. Assert the whole refusal with `exactly()`. Make a fake
*record* its arguments. Do not let it accept any arguments. A fake that is
declared as `lambda *_: value` proves only that a call happened.

**Source simplifications, where the mutant was equivalent because the code was
redundant.** Each simplification removed the mutant by removing the redundancy.
Each one is an improvement on its own terms:

- `shards_root`, `jobs_root`, and `project_source_root` give each of the three
  owned directory names one definition. One test pins them by value. A
  filesystem assertion cannot pin the spelling of a path on a case-insensitive
  volume. For this reason `shards` versus `SHARDS` survived for a long time.
- `_verify_derived_fields` compared six payload fields with the values that it
  had just given to the constructor. Only the four `init=False` fields can
  disagree.
- `process_shard` built a checkpoint that it never used on the only path that
  reached the call.
- `_run_directory_exists` returned a bool that nobody read. It is now a
  `_require_regular_run_directory` validator.
- `validate_remote_path` rejected a `.` component that `PurePath` had already
  dropped while it parsed.
- `_run_wide_intent_block` skipped the intent file of its own job two times. A
  job directory has the name of its bundle. `_iter_intents` already refuses an
  intent that does not bind to the bundle of its directory.
- `prepare_job` validated the same three limits two times. It did this once
  through `render_job_script` and once through `_job_config_payload`.
- The three callers that validate an annotation table without a reservation of
  its identities now say so by name. They use
  `validate_annotation_table_without_reserving`. Before, they passed
  `merge_seen=False`.
- `quarantine_orphan_artifacts` takes `_both_state_locks` directly. It does not
  pass a constant `submission_locked=False`.

**Pragmas, only with a written line-specific proof.** The team added six. The
list below shows them with the others.

#### Two traps to record

`ruff format` moves a trailing comment onto a closing bracket. There, a
line-scoped pragma no longer annotates the statement for which the team wrote
it. Worse, mutmut honours a trailing pragma only on a *single-line* statement.
It records the line where the statement *starts*. Thus it ignores, without a
message, a pragma that is written against one argument of a multi-line call.
Three pragmas that the team added during this work did nothing for this reason.
The team replaced them with the source changes above. To check a new pragma,
confirm that the mutant that it targets is not generated again.

The second trap is the test selection of the gate. The per-test coverage
contexts override the recorded association of mutmut. Thus a *stale* context
file hides newly added tests from the selection. The tests never run. The
mutants that they kill stay as survivors. After you add tests, run `just
mutation-contexts` again. When test ids change, delete
`mutants/mutmut-stats.json` and `mutants/mutmut-recorded-tests.json`. If you do
not, a renamed parametrisation stops the clean preflight of the run with `not
found:`.

#### Equivalence classes that the team excludes and does not kill

Each one is visible in the source and has a line-specific proof. A pragma has a
line scope. Thus it also suppresses every other mutation of that line. Where
this cost was real, the team removed the redundancy instead, as the list above
shows.

- `# pragma: no mutate - static cast`: `typing.cast` erases its type argument at
  runtime. Thus `cast(None, x)` cannot change the behaviour.
- `# pragma: no mutate - codec alias only`: codec names are case-insensitive.
  Thus `"UTF-8"` and `"utf-8"` resolve to the same codec.
- `# pragma: no mutate - digest names are case-insensitive`: `hashlib` resolves
  `"SHA256"` and `"sha256"` to the same algorithm.
- `# pragma: no mutate - newline domain is LF or CRLF`: the value comes from the
  front-matter regex `\\r?\\n`. Thus the non-LF branch is the only supported
  alternative that remains.
- `# pragma: no mutate - distinct find offsets`: the two distinct markers return
  `-1` or their non-negative position independently. Thus a change to one
  comparison cannot make the combined boundary check different.
- `# pragma: no mutate - rfind starts at zero`: if you omit the start argument of
  `str.rfind`, it is the same as `0` for this line-prefix lookup.
- `# pragma: no mutate - same falsy default`: `or ""` normalizes a missing or
  null Hub SHA. Thus the `getattr` defaults `""` and `None` are identical.
- `# pragma: no mutate - no upper-case X can occur`: the stem is lower-cased
  before the tool strips it. Thus stripping `"-"` and stripping `{"X", "-"}`
  remove the same characters.
- `# pragma: no mutate - reassigned before its first read`: the loop assigns the
  two row-group alignment variables in its first iteration. With no row groups,
  the loop never runs and the early return reads neither variable.
- `# pragma: no mutate - equal or refused either way`: the remote bundle root
  comes from one of three siblings. Their parents must already agree. The check
  below refuses the chosen parent when they do not agree.
- `# pragma: no mutate - any fixed offset`: `astimezone` makes the offset fixed.
  Thus the addition is absolute and not wall-clock. Each fixed-offset zone gives
  the same instant. The code reads the result only through
  `is_weekday_daytime`.
- Two `# pragma: no mutate start` / `end` blocks cover two items. The first is a
  provisional `identity_sha256` that `UploadPlan.to_payload` omits by design.
  The second is a dry-run delegation whose `apply` argument is already falsy on
  that path.

#### The gate uses selection and not guesswork

The cost used to be the test selection of the gate. Mutmut associates a function
with the tests that entered its trampoline. 221 of 1 096 functions had no
recorded test. This forced the gate to run the whole suite of 2 618 tests for
each of their mutants.

Per-test coverage contexts give the same association exactly. Mutmut mutates
function bodies. A test that never executes a line of a function cannot observe
the mutation of that function. Thus the covering tests are exactly the tests
that can kill its mutants. `just mutation` now records that map first (`just
mutation-contexts`). The gate uses it.

Measured effect on a full sweep:

| | Before | After |
| --- | --- | --- |
| Test executions | 748 103 | 149 525 (**5.0x fewer**) |
| Selection per function, median | 161 | 79 |
| Selection per function, mean | 683 | 136 |
| Worst selection | 2 618 | 737 |
| `_cascade_fingerprint` | 2 618 | 73 |
| `glotlid._score` | 2 618 | 38 |

The map uses the mangled names of mutmut as keys. The tool derives them from the
AST. The map covers all 1 096 names that it generates. A contract test pins that
shape, so the fast path cannot degrade without a message. Where the coverage
says nothing about a function, the previous selection stays. It is always sound
to run more tests.

The team already verified these mechanics. Nobody needs to diagnose them again:

- The escalation ladder is sound and cheap. For each cluster that the team
  checked, the killing test is inside the stage-two selection of 40 tests. Thus
  killable mutants do not pay for the full suite.
- `mutmut._run` **does** execute again a mutant that already has a cached exit
  code. Thus a resumed run uses newly added tests. The team confirmed this
  directly: `models.x__cascade_fingerprint__mutmut_1` went from exit code 0
  (survived) to 1 (killed) on a new run with no source change.
- The *true* survivors dominate the runtime. A function with no recorded
  trampoline hit runs the whole suite of 2 618 tests in the final stage. Thus
  each surviving mutant costs minutes. With about 900 survivors, a full
  convergence run takes several hours. The candidate is on the external drive.
  This makes it slower.

#### Run the gate and keep the run honest

WARNING: Do not interrupt the gate. Twice, a kill during a write truncated a
`.py.meta` file. This drops the results of that file without a message. If you
must stop the gate, do these steps afterwards. Look for zero-byte `.py.meta`
files. Delete them. Then mutmut generates them again.

Convergence from 624 survivors took eight passes on an SSD scratch copy. Repeat
the sequence that finally worked exactly:

1. Run the suite. Then run `just mutation-contexts` again. It runs across
   processes: 5m04s changed to 2m37s here. Every shard waits for it.
2. Delete `mutants/mutmut-stats.json` and `mutants/mutmut-recorded-tests.json`.
3. Run the gate.
4. Confirm with `scripts/check_mutation_score.py --minimum-score 100`.

The scoped pull-request gate reads the same map. It used to select the tests by
the test files that the branch touched. This is slower and also weaker. On a
branch that changed 69 test files and 303 source functions, that rule ran 2,047
tests for each mutant. This is 620,241 test-executions for one mutant each. The
map gives 20,745. In the old rule, a mutant that only an untouched test could
kill was reported as a survivor.

Record the contexts in parallel and not serially. This is not only for the
time. Each worker starts with fresh module state. Thus a lazily imported name
that one process resolves one time and caches is resolved again in the other
processes. The team compared the two maps directly over 1,466 functions. No
function lost a covering test. Four `__getattr__`-style functions gained tests
that a single process never records. That shadowing is exactly what let a
`__getattr__` mutant survive a test that read an attribute that was already
cached. Thus the parallel map is the sounder input and also the faster input.

#### Read a survivor without running the gate

The loop above is for *proving* a score. It is the wrong tool for a question
that occurs much more often: "what does this survivor change?" The source of a
mutant is a by-product of a full run. A full run takes tens of minutes in CI. A
local run must first go through the stats phase of mutmut.

`mutate_file_contents` is a pure function of the text of one file. Thus the diff
is available without the execution of anything:

```
python scripts/show_mutant.py \
    osm_polygon_description_tag.dataset.stats.x_collect_stats__mutmut_24
```

You can paste the names directly from the log of a shard, several at a time. The
answer arrives in seconds. Use this to sort the survivors into "needs a test" and
"equivalent". Keep the gate to confirm the result.

The ids are positions in a list for each function. Thus any edit to the file
numbers them again. After you change the source, read the survivor names again.
An id that you carry across a commit points at a different mutation.

Give the gate a `TMPDIR` that no other process uses. If the gate shares a
`TMPDIR` with an ad-hoc `pytest` run, the run deletes the numbered directory
under the gate. Then its stats collection stops with `FileNotFoundError`.

Mutmut executes again a mutant that already has a cached exit code. Thus a
resumed run uses newly added tests. The team confirmed this directly:
`models.x__cascade_fingerprint__mutmut_1` went from exit code 0 (survived) to 1
(killed) on a new run with no source change.

Source changes here make a prepared Grid snapshot invalid. `snapshot_id` binds
`fingerprint_project_source`. This work changed `src/`. Thus you must freeze the
snapshot again before the run below. Step 1 there already lists this.

## Executed

### Grid'5000 full-dataset run

The run is complete, under the **ungated** detection policy. Snapshot
`0051cc7ce300e58e65829dead01e7edfda46fa7a0e76fb992e1928f61177ffdb`, model
configuration fingerprint
`1d6f31e245a922d89d6d341f24cf0db2304160b2b7ce4f5d8eb890eef148c9a7`, frozen over
all 386 shards.

The team drove the run as eight cooperating drivers, one for each site (`nancy`,
`lille`, `lyon`, `grenoble`, `luxembourg`, `nantes`, `sophia`, `toulouse`). Each
driver owned a disjoint `--shard-stride 8` share and its own run directory. The
run-wide lock refuses a shared run directory. Each driver reached `run_end` with
no halt.

| Measure | Value |
| --- | --- |
| Shards complete | 386 / 386 |
| Source rows processed | 906 631 / 906 631 |
| Annotations produced | 919 126 |
| Base `description` values | 887 077 |
| Localized `description:*` values | 32 049 |
| Detected | 885 740 |
| Uncertain | 27 512 |
| Non-linguistic | 5 874 |
| Distinct languages assigned | 278 |
| Split into sentences | 840 897 |
| Sentences produced | 978 773 |
| Eligible for splitting (detected) | 885 740 |
| Splitting coverage of eligible units | 94.9372 % |
| Distinct languages outside the splitter's 85 | 192 |
| Skipped, language unsupported by the splitter | 44 843 |
| Skipped, no language detected | 33 386 |

The row totals and the annotation totals are identical to the totals of the
previous conservative run. This is the conservation check. The removal of the
gate changes *which* language a value gets. It does not change how many values
exist. The coverage changes from 62.27 % to 96.37 % of the annotations. The
number of distinct languages *falls* from 324 to 278. Values that the tool
previously withheld as uncertain now resolve into common languages and not into
rare languages.

The team verified the result two times, independently. The first time was
`language validate`. The second time was a recomputation of the SHA-256 of each
part against its receipt. The team also checked that the receipt chain tiles
`[0, rows)` with no gap or overlap. It reconciled the annotation counts. It
asserted the snapshot and the model fingerprint on each checkpoint *and* each
receipt. Both checks report 386 / 386 and zero issues.

These two items also come from the original record:

- Sentences: 649 186
- Validation issues: none

The three category triples each sum to the annotation total. This is the
cheapest end-to-end consistency check available: 887 077 + 32 049, and 572 328 +
340 924 + 5 874, and 534 731 + 37 597 + 346 798 all equal 919 126.

The ten most frequent assigned languages, by annotation count: `eng` 202 847,
`deu` 92 452, `fra` 34 427, `por` 33 993, `rus` 33 185, `pol` 29 694, `spa`
22 510, `vec` 19 461, `nld` 13 196, `ita` 12 287.

**Sites.** Eight operator sites carried the run: `nancy`, `grenoble`, `lille`,
`lyon`, `nantes`, `sophia`, `toulouse`, `luxembourg`. The team excluded `rennes`
because its home directory was at about 24.8 GiB of a 25 GiB quota. Each job
used one core with a walltime of 30 minutes at most. There was one active job for
each site. The team never submitted a job again while another job could still
exist.

A run directory has a single writer by design. Thus the sites did not share one.
Each site took its own run-directory clone over the *same* snapshot and a
disjoint share of the shards. The team merged the clones into the master before
the export. A shard that was too large for one job committed an input cursor and
resumed in the next job.

**Recovery to record.** Two shards finished on their compute nodes, but nobody
collected them. The local helper that drove them stopped between the end of the
job and the retrieval of the results. Their durable intents stayed on the copies
of the run on the *sites*, with `terminal_state` unset. This is exactly the case
for which the fail-closed rule exists: `submit` refuses to retry a job that can
still exist. The team reconciled each shard with `grid status --apply` and then
collected it. This recovered both shards without a new computation. They are
`us-kansas` (468 rows, 468 annotations) on `lyon` job `2066906`, and
`us-missouri` (892 rows, 892 annotations) on `nantes` job `337775`.

**A merge trap.** The master has a placeholder checkpoint for each shard from the
moment of staging. The status is `paused`, the cursor is 0, and there are zero
annotations. A merge that uses `rsync --ignore-existing` *keeps the placeholder*.
It drops the completed per-site result without a message. The run then stays
incomplete for no visible reason. The merge must prefer a `complete` checkpoint
over a non-complete checkpoint. It must never do the reverse.

### Hugging Face publication

The team published and verified the data.

| Field | Value |
| --- | --- |
| Repository | `NoeFlandre/osm-polygon-description-tag` |
| Baseline revision | `1c417fb242e8ef5b6cf6f62d5bf7aba14914c386` |
| Data revision | `861c51c1488f4485a986cfe2683b3ea543e08faf` |
| Published revision (corrected card) | `d144ca6ae1dad4aefefbf92241da9cd8b097a7f7` |
| Published revision (splitting coverage) | `0eda1fd0d42a2b415cdbf950dca7047775a3895b` |
| Files uploaded | 388 (386 Parquet + `stats.json` + `export-manifest.json`) |
| Bytes under `language-v1/` | 103 531 140 |
| Files verified by size and SHA-256 | 388 / 388 |
| No-op rerun | revision unchanged, zero uploads |
| Outstanding issues | none |

The upload is additive. Everything goes under `language-v1/`. The same commit
adds a `language-v1` configuration that lists its Parquet paths explicitly. It
does not use a wildcard. The same commit also adds a generated section to the
root `README.md`. The `default` configuration and each existing file stay
unchanged.

The card that the team published with the data first still described the removed
confidence gate. It reported only the split total. The team corrected both and
published again as revision `d144ca6a`. Now the card states that the detection
is not gated on confidence. It publishes both reasons why a value stays unsplit.
It publishes the share of detected values that the splitting covers. It
publishes the most frequent detected languages. The correction produced a
*different* plan identity. This is the behaviour that the fix for the defect
below gives.

Immediately after the data upload, the Hugging Face Dataset Viewer reported the
`language-v1` configuration as missing. This was an indexing lag on the Hub
side. It was not a publication fault. The card already declared the
configuration. The Viewer showed it after the indexing was complete. Until then,
the tool recorded the publication as `unverified`. This is the correct
behaviour. The state changed to `verified` on a new run. The tool did not force
it.

An earlier defect is important to keep on record. `upload` commits the data
files *and* the rendered card in one commit. But the plan identity covered only
the files. Thus a change to only the card produced the same identity. The
publication resumed its recorded outcome, skipped the upload, and returned
`verified`. It verified against the *old* revision, with the stale card still on
the Hub. A green status while the artifact is unchanged is the worst form of
this failure. The team found it only when it fetched the published card and
compared it. The identity now covers the card section. Thus a changed card is a
different publication.

The first apply returned `unverified` with a single issue. The Dataset Viewer had
not yet shown the new configuration. This was indexing latency. It was not a
failed upload. The tool had already verified all 388 files by digest at the
resulting revision. The team ran publish again after the viewer caught up. The
command verified the same revision. It did not upload again. It returned
`verified` with no issues.

The dataset card states clearly that `top_score`, `runner_up_score`, and
`margin` are raw detector scores. They are not calibrated probabilities. The card
also states that **nobody measured the accuracy** on this dataset, because no
ground-truth labels exist for it. The card makes no claim of an accuracy figure.

## Run the suite from the mounted volume

Keep the temporary directories **outside** the project tree. Several tests go up
from a temp path to find `pyproject.toml`. If `TMPDIR` is inside the repo, they
find the real file. A directory such as `/tmp/osm-pdt` works.

Two end-to-end job-script tests skip when the temp path contains a shell
metacharacter. Each absolute path on a volume with the name `Seagate M3` does
this. This is not a defect. A remote path cannot contain shell metacharacters,
because OAR evaluates the stored command through a shell. To run these tests,
point `TMPDIR` at a plain path.

## Performance notes

The team profiled before it changed anything. The profile argued against any
change:

| Measurement | Value |
| --- | --- |
| Warm throughput | ~307 rows/s, ~320 detections/s |
| `detect_multiple_languages_of` | 66.5 % of warm time |
| `compute_language_confidence_values` | 23.4 % |
| Our Python wrapper | ~10 % |
| Cold start (Lingua's 293 MB of models) | 57–95 s per process |
| Description extraction | 0.8 % |

The team measured three candidate optimisations and rejected them:
whitespace-normalised cache keys (0.17 % of calls), a larger LRU (4 096 entries
wastes 321 re-detections, about 1 s across the six largest shards), and
Arrow-level tag pruning (the extraction is 0.8 %). The remaining cost in the
repository is the double validation of confidence values (~8.5 %). The public
contract of `compute_language_confidence_values` depends on it.

The cold start is the main structural cost. The whole-dataset inference takes
about 49 minutes warm. 386 one-shard jobs pay about 6.4 hours of repeated model
loading. To reduce that cost, a job must process several shards. This gives up
the one-shard-for-each-job property.
