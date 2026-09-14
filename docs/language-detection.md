# Language detection runbook

This page covers the additive `language-v1` annotation pipeline: what it
produces, how to run it locally, how to run one bounded Grid'5000 job, and how
to publish the result. The default dataset, its GeoParquet files, and schema 3
are never modified by anything described here.

## What is annotated

The counting unit is **one description value, not one polygon**.

For every row of the source GeoParquet, the exact `description` tag and every
`description:<suffix>` tag is read from the authoritative raw `tags` column. An
object carrying a base `description` and two localized values contributes three
annotation rows.

- Localized suffixes are treated as **opaque**. `description:fr` is not assumed
  to be French; the annotation describes the text, not the key.
- Null tag values are skipped. Empty and whitespace-only values are kept and
  classified as `non_linguistic`.
- The original text is preserved byte-for-byte in the output.

Because a single object can carry several description values, the number of
annotations is expected to exceed the number of polygons. Derive the actual
figure from `language-v1/stats.json` after a run; do not assume it.

## Output schema

One row per description value, in `language-v1/data/*.parquet`:

| Column | Type | Meaning |
| --- | --- | --- |
| `description_identity` | string | SHA-256 over `osm_type`, `osm_id`, exact tag key, and text hash |
| `source_pbf` | string | Provenance only; not part of the identity |
| `osm_type` | string | `way` or `relation` |
| `osm_id` | int64 | OSM object identifier |
| `tag_key` | string | `description` or `description:<suffix>` |
| `original_text` | string | The exact original value |
| `text_sha256` | string | SHA-256 of the original text |
| `language_code` | string (nullable) | ISO 639-3, only when `status` is `detected` |
| `top_score` | float64 (nullable) | **Raw** detector score, clamped to 1.0 only where float32 rounding exceeded it |
| `runner_up_score` | float64 (nullable) | **Raw** runner-up score |
| `margin` | float64 (nullable) | `top_score - runner_up_score` |
| `status` | string | `detected`, `uncertain`, or `non_linguistic` |
| `reason` | string | Why that status was assigned |
| `snapshot_id` | string | The immutable input snapshot this row came from |
| `model_config_fingerprint` | string | Detector, policy, splitter, and the splittable language set |
| `split_status` | string | `split`, `unsupported_language`, or `not_detected` |
| `split_reason` | string | Why that split status was assigned |
| `sentence_count` | int32 | Number of sentences; `0` whenever nothing was split |
| `sentences` | list\<string\> | The sentences, verbatim; empty unless `split_status` is `split` |

## Detector pipeline

The production detector is a deterministic cascade:

- Lingua 2.2.0 is the primary detector and applies the conservative policy.
- The pinned GlotLID v3 model is called only when Lingua returns `uncertain`.
- If GlotLID also returns `uncertain`, the original Lingua result is retained.
- A fallback-resolved row has `reason=fallback_glotlid_v3`; all other reasons
  retain their normal meaning.

One detail of the fallback's arithmetic is visible in the data. fastText
accumulates its softmax in float32, so a confident prediction comes back
marginally over 1.0 --- 1.0000100135803223 was observed on 12 of 194 Afghan
descriptions. A probability above one is rounding, not a score, so values
within 1e-4 of 1.0 are clamped to exactly 1.0 and anything beyond that is still
refused. Scores inside the interval are never altered.

The fallback artifact is `cis-lmu/glotlid`, file `model_v3.bin`, revision
`85cd6716494360367b75f642b5bc78667605d0b4`, with SHA-256
`a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e`. Its
Linux runtime is pinned to `fasttext-numpy2==0.10.2`. The snapshot records this
identity, so a run cannot silently switch models.

## Sentence splitting

Splitting runs in the same pass as detection, not as a second sweep. Cold start
dominates a Grid'5000 run --- 386 one-shard jobs pay about 6.4 hours of repeated
model loading against roughly 49 minutes of warm inference --- so a separate
stage would nearly double the cost to recompute something that is already in
memory: splitting is a pure function of the text and the detection result.

The splitter is SaT-3l-sm (`segment-any-text/sat-3l-sm`, revision
`137da054051ad9f1eac42025f758db4ac9f22535`, `model.safetensors` with SHA-256
`3e19cb0e5dbe9790d37d918d7e87880cb6577d497833f0f0627d80ae6ca1fe90`), run
through `wtpsplit==2.2.1`.

**A description is split only when its language was detected *and* the splitter
was trained on that language.** SaT is language-agnostic at inference --- it
takes no language argument, and `lang_code` in the library's API selects a style
adapter this configuration does not use --- so the 85 languages of its
supervised mixture are where it is *known competent*, and that list is the gate.
It is pinned in source rather than read from the installed library, and its
digest is part of `model_config_fingerprint`, so widening or narrowing it
changes the run identity instead of quietly changing the dataset.

Detection reports ISO 639-3 and SaT names its languages in ISO 639-1 (except
Cebuano, which has no 639-1 code), so the two are joined by an explicit table
that also maps the macrolanguage members a pinned detector actually emits ---
`nob` and `nno` to `no`, `arb` to `ar`, `cmn` to `zh`. A code the table does not
cover is unsupported. There is no fallback and no guessing.

Every row therefore carries one of three outcomes:

| `split_status` | When | `sentences` |
| --- | --- | --- |
| `split` | Detected, and SaT was trained on that language | The sentences, possibly none |
| `unsupported_language` | Detected, but outside SaT's 85 | Empty |
| `not_detected` | `uncertain` or `non_linguistic` | Empty |

`split_reason` names the specific case: `split_sat_3l_sm`,
`unsupported_language_<iso 639-3>`, or `not_detected_<detection status>`. A
skipped description is still a complete row and never makes a run incomplete.

Croatian is the clearest example of the gate doing real work: Lingua detects it
confidently, and SaT was never trained on it, so those descriptions are
published unsplit with `unsupported_language_hrv`.

## Limitations

- `top_score`, `runner_up_score`, and `margin` are **raw detector scores, not
  calibrated probabilities**. Do not read them as confidence percentages.
- **No accuracy has been measured on this dataset.** The detector was chosen
  for its documented suitability on short text and has not been benchmarked
  here. Treat every label as an unvalidated annotation.
- Short values are frequently `uncertain` by design. A conservative minimum
  letter count, minimum score, and minimum margin are applied before any
  language is assigned.
- When mixed-language evidence is identified, the result is `uncertain` with
  reason `mixed_text`. The detector may miss mixed-language text, especially
  short values.
- Languages outside the detector's supported set cannot be identified reliably.
  They may be marked `uncertain` or incorrectly assigned a supported language.

## Local setup

The detector is an optional extra so the core dataset build stays free of it:

```bash
uv sync --frozen --extra language
```

## Preparation versus execution

The pipeline separates freezing inputs, spending a processing budget, and
auditing the result, so each is independently repeatable.

### 1. Freeze the input snapshot

```bash
uv run osm-polygon-description-tag language prepare --source-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root/data" --run-dir "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root/language-run-lingua-glotlid-v3-full" --project-root .
```

This writes `snapshot.json`, binding every source Parquet's relative path,
size, SHA-256, schema identity, and row count, together with the detector
configuration and the code and `uv.lock` fingerprints.

The snapshot identity deliberately **excludes the absolute source root**, so
staging a shard on another machine does not change it. Re-running `prepare`
with unchanged inputs is idempotent; if any source file changed, it fails
rather than silently rewriting the identity.

### 2. Process one shard

```bash
uv run osm-polygon-description-tag language run \
  --source-root <staged-source> --run-dir <run-dir> --project-root <staged-project> \
  --shard region.parquet --batch-size 512 --budget-seconds 1200 \
  --glotlid-model-path <model_v3.bin> \
  --sat-model-path <sat-3l-sm-dir>
```

`run` requires only the selected shard's file to be present, not the whole
dataset. It streams projected Arrow batches (`source_pbf`, `osm_type`,
`osm_id`, `tags`) and never loads a whole file or the whole dataset.
Batch processing and the repeated-text cache are bounded. Exact duplicate
detection still retains description identities: memory grows with the number
of annotations in the shard (or selected run during full validation), rather
than remaining constant for arbitrarily large inputs. Choose small shards;
the batch-size setting alone does not bound this identity bookkeeping.
Before inference, the current project source and lockfile must match the frozen
fingerprints. The detector is built with the snapshot's language scope and its
configuration fingerprint is checked as well. `--project-root` defaults to the
current working directory.

The run directory is locked for the attempt, so a second local worker is
refused rather than interleaving commits.

### 3. Audit what was produced

```bash
uv run osm-polygon-description-tag language validate --run-dir <run-dir>
```

`validate` is strictly read-only. It never repairs, deletes, or rewrites
anything; it reports what is on disk and leaves the decision to you.

## Exact resume and corruption behaviour

Each batch is committed in a fixed order: the annotation part, then its
receipt, then the checkpoint. Every write fsyncs its contents, renames
atomically, and then fsyncs the directory.

**The checkpoint is the single source of truth.**

| Interruption point | Behaviour on the next run |
| --- | --- |
| Before the part is committed | The batch is reprocessed from the last checkpoint |
| After the part, before the receipt | The part is deterministically rewritten; nothing is adopted |
| After the receipt, before the checkpoint | Same; the checkpoint does not list it, so it is redone |
| After the checkpoint | Resumes from exactly that cursor |

Part names are derived from the input row offset, so a redone batch overwrites
its own file rather than creating a duplicate. Uninterrupted output and
paused-then-resumed output are byte-identical.

On resume the worker verifies every part the checkpoint lists: the file must
exist, hash to its receipt, parse as the frozen annotation schema, and hold the
recorded number of rows. Anything else is refused. A checkpoint from a
different snapshot, detector configuration, shard, row count, or batch size is
rejected rather than adopted.

Exhausting the processing budget returns a **paused** state, never a completed
one. Real errors propagate; they are never converted into a completed result.

## Grid'5000

!!! warning "Preflight is mandatory before every run"
    Account entitlement, current quota, site capacity, and live scheduler
    state are never assumed. They remain mandatory checks before each
    authorised run, and the preflight fails closed on anything it cannot
    positively interpret.

!!! success "Execution status"
    **The full dataset has been processed and published.** Snapshot
    `9a03d00020191df375c25d3e4fa9b79e28ac9f8d83868d274a508ab954a84e98` ran to
    completion across all 386 shards: 906 631 source rows into 919 126
    annotations, validating `complete` with no issues. It is published at
    revision `710bd78400b84d7a0cc6291e0bd2dff15f043985` of
    `NoeFlandre/osm-polygon-description-tag`, 388 files under `language-v1/`,
    all verified against the Hub by size and SHA-256.

    Eight sites carried the run --- `nancy`, `grenoble`, `lille`, `lyon`,
    `nantes`, `sophia`, `toulouse`, `luxembourg` --- one core and at most a
    30-minute walltime per job, one active job per site throughout. `rennes`
    was excluded on home quota. Full counts, per-language totals and the
    publication record are in
    [the rollout status](language-rollout-status.md#executed).

    Five things were found only by running it, and all are fixed:

    - the multi-shard driver read `outcome` at the top level of the submit
      payload, where the CLI nests it under `result`, so a genuinely queued job
      was reported as an unclean submission;
    - `AutoTokenizer` cannot resolve a tokenizer class from SaT's `xlm-token`
      model type, so the tokenizer needs its own directory carrying XLM-R's
      config, or `pad_token_id` is `None` and inference dies mid-batch;
    - the submission intent is written on the *site's* copy of the run, so it
      has to be adopted back before a shard can be acknowledged at all;
    - fastText accumulates its softmax in float32 and returns probabilities
      marginally over 1.0, which the score guard refused outright;
    - sites disagree on what a bare `oarsub` means: several auto-select a queue
      that does not exist and reject the job, while `sophia` refuses an explicit
      queue, so the queue is a per-site input.

    Two operational traps are worth carrying forward. A driver interrupted
    between a job terminating and its results being fetched leaves a terminal,
    unacknowledged intent on the site; `submit` then refuses to retry, correctly,
    and the shard must be reconciled with `grid status --apply` and collected
    rather than resubmitted. And the master run holds a placeholder checkpoint
    (`paused`, cursor 0) for every staged shard, so a merge using
    `rsync --ignore-existing` keeps the placeholder and silently drops the real
    result.

    The NumPy baseline blocker described below is resolved: every frontend
    reports zero x86-64-v2 flags, and pinning the *operator* environment to
    `numpy==1.26.4` is enough, because inference uses the locked environment
    built inside the job.

    Production data stays under the requested Seagate project root; it only
    gets there when the workflow is run with those paths.

### Job shape

| Constraint | Value |
| --- | --- |
| Cores | exactly 1 (not an exclusive node) |
| Walltime | at most 30 minutes |
| Useful processing | at most 20 minutes, leaving setup and termination margin |
| Concurrency | 1 |
| Staging | one shard |
| GPU, job arrays, speculative submission, automatic resubmission | none |

Dependency installation and inference both happen **inside the job**, on
allocated compute resources. Frontends are used only for lightweight file
management and scheduler operations.

### Policy safeguards

The preflight fails closed. Public Grid'5000 documentation does not define a
stable `usagepolicycheck` JSON schema, nor an exit-code contract meaning "your
quota permits this submission", so:

- **Exit status zero is not treated as approval.** Only the fields actually
  observed in a real capture (`start_time`, `stop_time`, `jobs`, `total_jobs`,
  `limits`) are read. No "remaining quota" or "approved" field is invented.
- Anything that cannot be positively interpreted becomes `unknown`, and
  submission under `unknown` is refused.
- Active jobs are counted from the selected site's `oarstat -u -J` JSON job
  map, separately from historical usage totals. This is not a cross-site
  inventory; check any reservations at other sites during the operator
  preflight as well. Unknown job states do not count as an empty account.
  The command adapter accepts the successful zero-byte no-jobs response used
  by OAR 2.5.9, 2.5.10, and 2.6.1, as well as the `{}` response used by OAR3.
  It does not interpret blank output from failed commands as evidence, and
  rejects the older OAR 2.5.8 `null` response. Unnamed jobs remain in the active
  count but are ignored when resolving a particular job name. These contracts
  follow the upstream
  [OAR command implementation](https://github.com/oar-team/oar/blob/debian-upstream/2.5.10/sources/core/qfunctions/oarstat)
  and [OAR3 implementation](https://github.com/oar-team/oar3/blob/master/oar/cli/oarstat.py).
- Home quota is read from `quota -p -w`. `quota -l` is deliberately not used
  because it excludes NFS home storage. Both block and file-count limits are
  checked, including raw grace fields and NFS device paths; truncated home
  rows make the evidence unknown. The parser follows the upstream
  [quota-tools output format](https://kernel.googlesource.com/pub/scm/utils/quota/quota-tools/+/refs/tags/v4.07/quota.c).
- Weekday daytime in Europe/Paris (09:00–19:00) is **blocked by default**,
  because daytime accounting is not verifiable from public documentation. This
  is a conservative default, not a permanent restriction: pass
  `--allow-daytime` once you have confirmed your own accounting. Note that the
  daytime quota is not a universal "two core-hours" allowance, short jobs are
  not automatically exempt, and besteffort privileges are not assumed.
- `night=noretry` is requested so a postponed night job is not silently retried.
- The entire requested walltime must fit within one day/night window. A job
  ending exactly at the boundary is allowed; one crossing it is refused.

### Workflow

Run scheduler and transfer commands from the chosen site's frontend, with
absolute paths in storage visible to the allocated compute job. The transfer
plans use filesystem paths, not an SSH hostname: they do not establish a
Mac-to-Grid connection. Arrange the authorised transfer to that site separately.
Do not SSH directly to a shared compute node; follow the site's OAR access
rules for a one-core reservation.

The operator's Python environment must already be installed; prepare it on
allocated compute resources, not by compiling dependencies on a frontend.
The examples use `uv run --no-sync` to prevent an implicit installation during
frontend operations. The generated job requires `bash` and `uv` on the compute
node; transfers require `rsync`, and scheduler operations require the site's
OAR and quota tools. Check these prerequisites before an authorised run.

!!! warning "Build the operator environment for the frontend's own CPU"
    An environment built on an allocated compute node can be unusable on the
    frontend that must run `oarsub`. A `nancy` operator environment built this
    way failed on the frontend with `NumPy was built with baseline
    optimizations: (X86_V2) but your machine doesn't support: (X86_V2)`, which
    breaks every `grid` subcommand before it reaches the scheduler. Build the
    operator environment on a machine whose CPU baseline the frontend also
    satisfies, and check it with a harmless
    `osm-polygon-description-tag --help` before staging anything.

    The cause is measurable rather than mysterious: `fnancy` reports a
    `Common KVM processor` whose `/proc/cpuinfo` flags contain `pni` and
    `cx16` but neither `sse4_2` nor `popcnt`, so it is an x86-64 baseline
    machine with SSE3. NumPy 2 wheels require x86-64-v2 and abort on import;
    NumPy 1.26 wheels have an SSE3 baseline and run. Pinning the *operator*
    environment to `numpy==1.26.4` is therefore enough, and it does not touch
    model semantics: the job script runs `uv sync --frozen --no-dev --extra
    language` on the allocated compute node, so inference always uses the
    locked environment. Verify the split holds before relying on it:

    ```bash
    ssh nancy 'grep -m1 flags /proc/cpuinfo | tr " " "\n" | grep -cE "^(sse4_2|popcnt)$"'
    # must print 0, which is why NumPy 2 cannot be used on the frontend
    ```

### Driving all 386 shards

`scripts/run_language_grid.py` sequences the per-shard protocol below; it adds
no policy of its own. It stages and transfers one shard, submits it through the
CLI's own apply gate, polls until the scheduler reports a terminal state, then
collects and acknowledges before touching the next shard. Anything reported as
ambiguous, active, or unresolved stops the run for an operator to reconcile,
and `--allow-daytime` is forwarded only when it is passed explicitly.

The driver exists because `grid stage --apply` emits a *filesystem* rsync argv:
it cannot reach the site from a workstation. The driver performs that transfer
over SSH instead, which is the separately arranged authorised transfer this
runbook requires.

```bash
uv run python -m scripts.run_language_grid \
  --run-dir data-root/language-run-lingua-glotlid-v3-full \
  --source-root data-root/data --project-root . \
  --retrieval-dir data-root/language-retrieval \
  --ssh-host nancy --site nancy \
  --remote-bundle-root /home/nflandre/osm-language-grid-v3 \
  --remote-glotlid-model-path /home/nflandre/models/glotlid-v3/model_v3.bin \
  --remote-sat-model-path /home/nflandre/models/sat-3l-sm \
  --remote-operator-dir /home/nflandre/osm-language-grid \
  --remote-cli /home/nflandre/osm-language-grid/.portable-grid-probe/bin/osm-polygon-description-tag \
  --max-shards 1
```

Start with `--max-shards 1` and read the emitted JSON before widening it. The
driver is resumable: a shard whose checkpoint already validates as complete is
skipped, so re-running continues rather than repeating work.

### Stage the pinned models once

The cascade needs the pinned fallback model on storage the compute node can
read; `--glotlid-model-path` is an absolute path, and the job verifies its
SHA-256 before loading it. Fetch it once into the shared home and check the
digest against the pinned constant:

```bash
mkdir -p ~/models/glotlid-v3
curl -sSL -o ~/models/glotlid-v3/model_v3.bin \
  "https://huggingface.co/cis-lmu/glotlid/resolve/85cd6716494360367b75f642b5bc78667605d0b4/model_v3.bin"
sha256sum ~/models/glotlid-v3/model_v3.bin
# must print a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e
```

The artifact is about 1.6 GiB, so confirm `quota -p -w` has room before
fetching it. A digest that does not match the pinned constant must never be
used: the loader refuses it, and so should you.

The sentence splitter needs the same treatment, with one difference:
`--sat-model-path` is a **directory**, not a file. `wtpsplit` loads a model the
way `transformers` does, from a directory holding `config.json` beside the
weights, and it needs a tokenizer staged too --- the library's default would
fetch `xlm-roberta-base` from the Hub, and a compute node has no reason to have
network access. The job verifies the weights' SHA-256 before loading anything.

The tokenizer goes in its own `tokenizer/` subdirectory, with XLM-R's *own*
`config.json` beside it. This is not tidiness. SaT's `config.json` declares the
custom model type `xlm-token`, which no tokenizer class is registered for, so a
tokenizer loaded from the model directory resolves to a generic fast tokenizer
carrying no special tokens at all: `pad_token_id` is `None`, padding writes
`None` into the input ids, and inference dies part-way through the first batch
with a `TypeError` from deep inside `wtpsplit`. XLM-R's config names the real
tokenizer class, which is what makes `<pad>` resolve to id 1 --- exactly the
`pad_token_id` SaT's own config expects.

```bash
mkdir -p ~/models/sat-3l-sm
cd ~/models/sat-3l-sm
rev=137da054051ad9f1eac42025f758db4ac9f22535
for name in model.safetensors config.json; do
  curl -sSL -O "https://huggingface.co/segment-any-text/sat-3l-sm/resolve/$rev/$name"
done
# the tokenizer SaT expects, in its own directory with XLM-R's own config, so
# nothing is fetched at run time and the real tokenizer class is named
mkdir -p tokenizer && cd tokenizer
for name in config.json tokenizer.json tokenizer_config.json sentencepiece.bpe.model; do
  curl -sSL -O "https://huggingface.co/FacebookAI/xlm-roberta-base/resolve/main/$name"
done
cd ..
sha256sum model.safetensors
# must print 3e19cb0e5dbe9790d37d918d7e87880cb6577d497833f0f0627d80ae6ca1fe90
```

The weights are about 815 MiB and the tokenizer adds roughly 17 MiB, so budget
about 2.5 GiB of home quota for the two models together. Confirm the directory
loads before submitting 386 jobs:

```bash
python -c "from wtpsplit import SaT; d='$HOME/models/sat-3l-sm'; \
  print(SaT(d, tokenizer_name_or_path=d + '/tokenizer').split('A park. It has benches.'))"
```

Prepare a portable payload containing the project, lockfile, immutable
snapshot, exactly one source shard, and validated resume artifacts:

```bash
uv run --no-sync osm-polygon-description-tag language grid stage --run-dir <run-dir> --project-root <project> --source-root <source> --shard region.parquet --remote-bundle-dir /home/user/language-bundle \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin \
  --sat-model-path /home/user/models/sat-3l-sm
```

This creates local staging files and prints the exact transfer argument vector.
It neither transfers nor submits anything. Add `--apply` only when the printed
source and destination are correct and visible in the current filesystem.
After collection, stage again to include the newly committed checkpoint before
an explicitly requested continuation.

```bash
uv run --no-sync osm-polygon-description-tag language grid prepare --run-dir <run-dir> --shard region.parquet --remote-project-dir /home/user/project --remote-source-dir /tmp/staging/source --remote-run-dir /tmp/staging/run \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin \
  --sat-model-path /home/user/models/sat-3l-sm
```

`prepare` is the lower-level script/metadata operation for already staged inputs;
unlike `stage`, it does not copy the project or source data. It binds the code
fingerprint, lock fingerprint, snapshot, and selected shard to the job script.
Prepared settings cannot be silently rewritten after staging.
The scheduler is invoked with an argument vector, and its final script argument
is shell-quoted separately: OAR later evaluates that stored command through the
user's shell. Spaces and metacharacters in a local script path therefore remain
part of the filename, not executable syntax.

```bash
uv run --no-sync osm-polygon-description-tag language grid submit --run-dir <run-dir> --shard region.parquet --site nancy --remote-project-dir ... --remote-source-dir ... --remote-run-dir ... --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin --sat-model-path /home/user/models/sat-3l-sm
```

Without `--apply` this only plans: it contacts no scheduler and prints the
exact `oarsub` argument vector along with the policy verdict. Adding `--apply`
gathers live policy evidence and submits.

**The submission intent is written durably before `oarsub` runs.** If the call
then times out or answers without a job identifier, the outcome is reported as
`ambiguous` and the intent on disk proves a job may already exist. Ambiguity is
never resolved by submitting again.

```bash
uv run --no-sync osm-polygon-description-tag language grid status --run-dir <run-dir> --shard region.parquet --apply
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run-dir> --shard region.parquet
```

`status` reconciles a recorded submission before anything else is attempted;
`collect` validates the returned checkpoints and parts.

To retrieve a completed attempt into a separate local staging directory, first
inspect the plan without `--apply`:

```bash
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run-dir> --shard region.parquet --remote-bundle-dir /home/user/language-bundle --retrieved-run-dir <retrieval-dir>
```

With `--apply`, the command retrieves only the selected shard's state, validates
it against the immutable snapshot, imports committed artifacts, and records
collection acknowledgment. A paused checkpoint is not completion. A later
attempt requires terminal scheduler reconciliation and validated collection;
an active or ambiguous attempt must never be bypassed by submitting again.
Import holds both submission and worker locks. It validates a private copy,
refuses backward progress or changes to already committed history, then commits
new parts and receipts before the checkpoint. An interruption leaves the old
checkpoint authoritative; retry collection with the same retrieved state.

Durable validated results belong on the external project storage. Never fall
back to internal storage if that mount is unavailable.

### Crash recovery

`prepare` and `stage` write a validated zero-cursor checkpoint for the shard
before a job can be submitted: `paused` for a shard with input rows, `complete`
for a zero-row shard. They take the run-wide submission lock first and the
exclusive worker lock second, always in that order. An existing valid
checkpoint is preserved, never rewritten; a corrupted, symlinked, conflicting,
or otherwise unexpected state fails closed, and a shard whose checkpoint cannot
be initialised cannot be submitted. A job that dies during setup, before it
processes a single row, therefore still leaves a checkpoint that `collect` can
validate and acknowledge, which is what makes one bounded retry safe. An
active or ambiguous submission is still never resubmitted; it must be
reconciled.

A worker that dies after writing a part or a receipt but before committing its
checkpoint leaves artifacts the authoritative checkpoint does not list. They
are never adopted and never deleted. `stage` moves recognised generated
orphans into `shards/<key>/quarantine/`, reports them in its JSON output as
`quarantined`, and then restages the checkpoint-listed state so the shard can
be collected and resumed. Anything that is not a recognised generated artifact
--- an unknown filename, a symlink, a FIFO --- is refused instead of moved,
and committed parts and receipts are never touched. The public `validate_run`
contract is unchanged: it still reports every artifact a checkpoint does not
account for as an issue rather than ignoring it. Commit ordering stays parts,
then receipts, then the checkpoint last.

## Publication

No Hugging Face upload has been performed for this implementation; the commands
below have only been exercised against local fakes.

Publication is additive. Data uploads are allowlisted under `language-v1/`.
The same atomic Hub commit adds the corresponding configuration and generated
section to the root `README.md`, preserving existing configurations, default
selection, and unrelated prose. Nothing deletes remote files.
The installed training configuration lists exactly the planned Parquet paths,
not a wildcard that could accidentally include stale or unrelated files.
An existing conflicting language configuration is refused rather than changed.

```bash
uv run osm-polygon-description-tag language export --run-dir <run-dir> --export-dir <export-dir> --card-section <path>.md
```

`export` refuses to run unless **every** snapshot shard validates as complete,
so a partial run cannot be published as though it covered the dataset. It
writes `language-v1/data/*.parquet`, `language-v1/stats.json`, and optionally
the generated dataset-card section. All counts come from the exported rows.
The final `language-v1/export-manifest.json` binds the data and statistics by
size and SHA-256. Missing manifests and changed files are refused, including
an interrupted re-export that leaves files from different attempts. Re-export
marks this manifest as in-progress before touching data; only a successful
retry restores a completed seal. Export and publication share a local lock;
publication rechecks the sealed files
after acquiring it and before contacting the Hub.

```bash
uv run osm-polygon-description-tag language publish --export-dir <export-dir> --repo NoeFlandre/osm-polygon-description-tag --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Publication is gated three times: the repository identifier must be repeated,
`--apply` must be passed, and `--baseline-revision` must match the repository's
current revision. A repository that moved is reported as `drifted` and refused
rather than overwritten.
Planning, including a drift refusal, never writes publication state.

After uploading, every planned file is verified against the Hub by size and
SHA-256 at the exact resulting revision, and the Dataset Viewer is checked for
the `language-v1` configuration. An upload whose success cannot be established
is recorded as `ambiguous`; the next invocation **verifies** rather than
re-uploading. The intent is persisted **before** the upload starts, including
for library calls that omit an explicit state path. Previously verified
publications are checked again; cached state cannot conceal later remote file
loss. An unresolved state belonging to a different plan is refused.

The adapter reads the existing card at the baseline revision and creates the
data-and-card commit with that revision as its optimistic concurrency guard.
Malformed or conflicting card configurations are refused. Do not replace the
existing `configs` list with a language-only list: that would hide the default
dataset. The optional `--card-section` file is a local preview, not a separate
manual publication step.

The Dataset Viewer is asynchronous and its `/splits` endpoint is unversioned.
Card metadata alone does not prove that `language-v1/train` is queryable.
Pending or failed indexing leaves publication unverified; rerun verification
later without repeating the upload. A matching repository head plus a ready
Viewer response is useful readiness evidence, not proof that the Viewer indexed
an exact commit.

## Reproducibility and provenance

### Quality-gate interpretation

Mutation reports count generated mutants, not mathematically equivalent program
variants. As elsewhere in this repository, narrowly marked serialization and
static-typing statements are excluded where mutmut cannot distinguish an
equivalent argument change: `ensure_ascii=False` versus `None`, the unused JSON
object-key separator in a flat identity array, and the type argument of
`typing.cast`. The LRU eviction call is also narrowly marked because
`OrderedDict.popitem(last=None)` has the same behavior as `last=False`;
recency and bounded eviction are tested explicitly. These exclusions are visible in source; exact Unicode bytes,
identity hashes, and decoded values remain covered by behavioral tests. A
survivor, timeout, or unchecked generated mutant is never counted as killed.

### Run identity

Every annotation row records the `snapshot_id` and `model_config_fingerprint`
that produced it. The snapshot in turn records the source file hashes, the
schema identity, the detector policy, and the code and lockfile fingerprints.

Lingua and its version are pinned and verified at construction time. For the
cascade, the configuration fingerprint also records the exact GlotLID repository,
revision, runtime, and model SHA-256; `binary_artifact_hash` is populated only
with that independently verified pinned artifact. Legacy pure-Lingua snapshots
may keep it unset.

The snapshot also records the splitter by name: `splitter_name`,
`splitter_revision`, and `splitter_languages_fingerprint`. These are bound into
`snapshot_id` anyway, through `config_fingerprint`, but a fingerprint cannot be
read --- someone opening `snapshot.json` has to be able to say which splitter
produced the sentences without recomputing a hash. They are verified whenever
they are present; a snapshot written before the fields existed has nothing there
to disagree with, so parsing it still works. Its `snapshot_id`, however, was
hashed over a payload without them and no longer verifies, so a run directory
frozen before this change must be re-prepared rather than resumed.

Detection is deterministic: scores within the fixed tie epsilon produce an
uncertain result without a language label, so the provider's ordering of tied
languages cannot change the annotation.

## Licensing and attribution

The annotated text is OpenStreetMap data, © OpenStreetMap contributors,
available under the Open Database License (ODbL). Language labels are derived
annotations produced with the pinned `lingua-language-detector` primary and the
documented GlotLID v3 fallback. The dataset card records both upstream
attributions and exact model provenance.
