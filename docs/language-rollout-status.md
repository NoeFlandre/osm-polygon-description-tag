# Language rollout status

A living record of the `language-v1` cascade rollout: what is finished, what is
not, and what the next operator has to do. Update it in the same commit as the
work it describes.

**Last updated:** 2026-09-20 · **Code:** `codex/repo-hardening-release`

## Summary

| Stage | State |
| --- | --- |
| Cascade implementation (Lingua primary, GlotLID v3 fallback) | **Done** |
| Sentence splitting (SaT-3l-sm, gated on the languages it was trained on) | **Done** |
| Local quality gates | **Done** |
| Mutation gate at 100 % | **Not met** — 20 708 / 20 775 killed (99.677 %), 67 survivors; see [#20](https://github.com/NoeFlandre/osm-polygon-description-tag/issues/20) |
| Grid'5000 operator environment | **Done** — the NumPy baseline blocker is resolved |
| Grid'5000 full-dataset run (ungated policy) | **Done** — 386 / 386 shards, 906 631 rows, 919 126 annotations |
| Hugging Face publication (ungated policy) | **Done** — revision `d144ca6a`, 388 files under `language-v1/` |

## Done

### Sentence splitting

Descriptions are split into sentences in the **same pass** as detection. Cold
start dominates a Grid run --- 386 one-shard jobs pay about 6.4 hours of
repeated model loading against roughly 49 minutes of warm inference --- so a
second sweep would nearly double the cost to recompute something already in
memory: splitting is a pure function of the text and the detection result.

A description is split only when detection settled on a language *and* the
splitter was trained on it. SaT-3l-sm takes no language at inference --- it is
language-agnostic, and `lang_code` in `wtpsplit` selects a style adapter this
configuration does not use --- so the 85 languages of its supervised mixture are
where it is *known competent*, and that list is the gate. Everything else is
published unsplit with the reason recorded: `unsupported_language_<iso 639-3>`
when the language is known but outside the 85, `not_detected_<status>` when
detection never settled. Croatian is the clearest case: Lingua detects it
confidently and SaT was never trained on it.

The supported set is pinned in source rather than read from the installed
library, and both it and the pinned artifact are folded into
`model_config_fingerprint`, so changing either changes the run identity rather
than quietly changing the dataset. Detection reports ISO 639-3 and SaT names its
languages in ISO 639-1, so the two are joined by an explicit table that also
covers the macrolanguage members a pinned detector emits (`nob`/`nno` to `no`,
`arb` to `ar`, `cmn` to `zh`). An unmapped code is unsupported; nothing is
guessed. See [the runbook](language-detection.md#sentence-splitting).

Two things about `wtpsplit` are worth recording, because both would have failed
only once a job was already running on a node:

- `SaT` loads a **directory**, the way `transformers` does, not a weights file.
  `--sat-model-path` is therefore a directory holding `config.json` beside
  `model.safetensors`, and the pinned SHA-256 is checked on the weights inside it.
- its tokenizer argument defaults to fetching `xlm-roberta-base` from the Hub.
  A compute node has no reason to have network access, so the tokenizer is
  staged into that same directory and named explicitly.

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

3 336 passed / 3 skipped, 99.49 % branch coverage, ruff format and lint, `ty`,
pre-commit, `uv lock --check`, `uv build`, wheel contents, strict MkDocs.

One skip is the Docker smoke test, which needs `RUN_DOCKER_SMOKE=1`. The other
two are the end-to-end job-script tests, which skip whenever `TMPDIR` contains a
shell metacharacter --- as every absolute path under `/Volumes/Seagate M3` does.
That is the guard working, not a gap: a remote path may not contain
metacharacters because OAR evaluates the stored command through a shell. Point
`TMPDIR` at a plain path to run them; see the note at the end of this document.

### Mutation gate at 100 %

`python scripts/check_mutation_score.py --mutants-root mutants --output
reports/mutation-summary.json --minimum-score 100` reports **100.00 %
(18 140 / 18 140)** with every unresolved bucket at zero: no survivor, timeout,
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

#### Reading a survivor without running the gate

The loop above is for *proving* a score. It is the wrong tool for the question
that comes up far more often -- "what does this survivor actually change?" --
because a mutant's source is a by-product of a full run: tens of minutes in CI,
and a local run that has to get through mutmut's stats phase first.

`mutate_file_contents` is a pure function of one file's text, so the diff is
available without executing anything:

```
python scripts/show_mutant.py \
    osm_polygon_description_tag.dataset.stats.x_collect_stats__mutmut_24
```

Names paste straight from a shard's log, several at a time, and the answer
arrives in seconds. Use this to triage survivors into "needs a test" and
"equivalent", and keep the gate for confirming the result.

The ids are positions in a per-function list, so any edit to the file renumbers
them. Re-read the survivor names after changing the source; an id carried across
a commit points at a different mutation.

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

## Executed

### Grid'5000 full-dataset run

Complete, under the **ungated** detection policy. Snapshot
`0051cc7ce300e58e65829dead01e7edfda46fa7a0e76fb992e1928f61177ffdb`, model
configuration fingerprint
`1d6f31e245a922d89d6d341f24cf0db2304160b2b7ce4f5d8eb890eef148c9a7`, frozen over
all 386 shards.

The run was driven as eight cooperating drivers, one per site
(`nancy`, `lille`, `lyon`, `grenoble`, `luxembourg`, `nantes`, `sophia`,
`toulouse`), each owning a disjoint `--shard-stride 8` share and its own run
directory; a shared run directory is refused by the run-wide lock. Every driver
reached `run_end` with no halt.

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

Row and annotation totals are identical to the previous conservative run, which
is the conservation check: ungating changes *which* language a value is given,
not how many values exist. Coverage moves from 62.27 % to 96.37 % of
annotations, and the distinct-language count *falls* from 324 to 278 --- values
that were previously withheld as uncertain now resolve into common languages
rather than rare ones.

Verified twice and independently: once by `language validate`, and once by
recomputing every part's SHA-256 against its receipt, checking that the receipt
chain tiles `[0, rows)` with no gap or overlap, reconciling annotation counts,
and asserting the snapshot and model fingerprint on every checkpoint *and* every
receipt. Both report 386 / 386 and zero issues.
| Sentences | 649 186 |
| Validation issues | none |

The three category triples each sum to the annotation total, which is the
cheapest end-to-end consistency check available: 887 077 + 32 049, and
572 328 + 340 924 + 5 874, and 534 731 + 37 597 + 346 798 all equal 919 126.

Ten most frequent assigned languages, by annotation count: `eng` 202 847,
`deu` 92 452, `fra` 34 427, `por` 33 993, `rus` 33 185, `pol` 29 694,
`spa` 22 510, `vec` 19 461, `nld` 13 196, `ita` 12 287.

**Sites.** Eight operator sites carried the run: `nancy`, `grenoble`, `lille`,
`lyon`, `nantes`, `sophia`, `toulouse`, `luxembourg`. `rennes` was excluded
because its home directory sat at roughly 24.8 GiB of a 25 GiB quota. Every job
was one core with a walltime of at most 30 minutes, one active job per site, and
no job was ever resubmitted while another might still exist.

Because a run directory is single-writer by design, the sites did not share one.
Each took its own run-directory clone over the *same* snapshot and a disjoint
share of the shards, merged into the master before export. A shard too large for
one job committed an input cursor and resumed in the next.

**Recovery worth recording.** Two shards finished on their compute nodes but
were never collected, because the local helper driving them was interrupted
between the job terminating and the results being fetched. Their durable intents
survived on the *sites'* copies of the run with `terminal_state` unset, which is
exactly what the fail-closed rule is for: `submit` refuses to retry a job that
may still exist. Reconciling each with `grid status --apply` and then collecting
recovered both without recomputing anything — `us-kansas` (468 rows, 468
annotations) on `lyon` job `2066906`, and `us-missouri` (892 rows, 892
annotations) on `nantes` job `337775`.

**A merge trap.** The master carries a placeholder checkpoint for every shard
from the moment it is staged: status `paused`, cursor 0, zero annotations. A
merge built on `rsync --ignore-existing` therefore *keeps the placeholder* and
silently discards the completed per-site result, leaving the run permanently
incomplete for no visible reason. The merge must prefer a `complete` checkpoint
over a non-complete one and never the reverse.

### Hugging Face publication

Published and verified.

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

The upload is additive: everything lands under `language-v1/`, and the same
commit adds a `language-v1` configuration listing its Parquet paths explicitly
rather than by wildcard, plus a generated section in the root `README.md`. The
`default` configuration and every existing file are untouched.

The card published with the data initially still described the removed
confidence gate and reported only the split total. Both were corrected and
republished as revision `d144ca6a`: the card now states that detection is not
gated on confidence, publishes both reasons a value goes unsplit, the share of
detected values that splitting covers, and the most frequent detected
languages. The correction produced a *different* plan identity, which is the
behaviour the defect below was fixed to give.

The Hugging Face Dataset Viewer reported the `language-v1` configuration as
missing immediately after the data upload. That was indexing lag on the Hub
side, not a publication fault: the configuration was already declared in the
card, and the Viewer exposed it once indexing completed. The publication was
left recorded as `unverified` until then, which is the correct behaviour --- the
state resolved to `verified` on re-run rather than being forced.

An earlier defect worth keeping recorded: `upload` commits the data files *and* the rendered card in one
commit, but the plan identity covered only the files. A card-only change
therefore produced the same identity, publication resumed its recorded outcome,
skipped the upload entirely, and returned `verified` --- against the *old*
revision, with the stale card still on the Hub. A green status while the
artifact is unchanged is the worst shape that failure could take, and it was
caught only by fetching the published card and comparing it. The identity now
covers the card section, so a changed card is a different publication.

The first apply returned `unverified` with a single issue --- the Dataset Viewer
had not yet exposed the new configuration. That is indexing latency, not a
failed upload: all 388 files already verified by digest at the resulting
revision. Re-running publish once the viewer caught up verified the same
revision rather than re-uploading anything, and returned `verified` with no
issues.

The dataset card states plainly that `top_score`, `runner_up_score` and `margin`
are raw detector scores rather than calibrated probabilities, and that **no
accuracy has been measured** on this dataset because no ground-truth labels
exist for it. Nothing in the card claims an accuracy figure.

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
