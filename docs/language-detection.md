# Language detection runbook

This page describes the additive `language-v1` annotation pipeline. It explains
what the pipeline produces. It explains how to run it locally, how to run one
bounded Grid'5000 job, and how to publish the result. Nothing on this page
changes the default dataset, its GeoParquet files, or schema 3.

## What is annotated

The counting unit is **one description value, not one polygon**.

For each row of the source GeoParquet, the tool reads the exact `description`
tag and each `description:<suffix>` tag. It reads them from the authoritative
raw `tags` column. An object with a base `description` and two localized values
adds three annotation rows.

- The tool treats localized suffixes as **opaque**. It does not assume that
  `description:fr` is French. The annotation describes the text, not the key.
- The tool skips null tag values. It keeps empty values and whitespace-only
  values. It classifies them as `non_linguistic`.
- The output keeps the original text byte-for-byte.

One object can have several description values. Thus the number of annotations
is expected to be larger than the number of polygons. After a run, get the
actual number from `language-v1/stats.json`. Do not assume it.

## Output schema

The output has one row for each description value, in `language-v1/data/*.parquet`:

| Column | Type | Meaning |
| --- | --- | --- |
| `description_identity` | string | SHA-256 over `osm_type`, `osm_id`, exact tag key, and text hash |
| `source_pbf` | string | Provenance only. It is not part of the identity |
| `osm_type` | string | `way` or `relation` |
| `osm_id` | int64 | OSM object identifier |
| `tag_key` | string | `description` or `description:<suffix>` |
| `original_text` | string | The exact original value |
| `text_sha256` | string | SHA-256 of the original text |
| `language_code` | string (nullable) | ISO 639-3. Set only when `status` is `detected` |
| `top_score` | float64 (nullable) | **Raw** detector score. The tool clamps it to 1.0 only where float32 rounding exceeded 1.0 |
| `runner_up_score` | float64 (nullable) | **Raw** runner-up score |
| `margin` | float64 (nullable) | `top_score - runner_up_score` |
| `status` | string | `detected`, `uncertain`, or `non_linguistic` |
| `reason` | string | The reason for that status |
| `snapshot_id` | string | The immutable input snapshot of this row |
| `model_config_fingerprint` | string | Detector, policy, splitter, and the splittable language set |
| `split_status` | string | `split`, `unsupported_language`, or `not_detected` |
| `split_reason` | string | The reason for that split status |
| `sentence_count` | int32 | Number of sentences. It is `0` when the tool did not split the text |
| `sentences` | list\<string\> | The sentences, verbatim. It is empty unless `split_status` is `split` |

## Detector pipeline

The production detector is a deterministic cascade:

- Lingua 2.2.0 is the primary detector. It applies only structural checks
  (length, ties, mixed text). The cascade does not gate the confidence.
- The tool calls the pinned GlotLID v3 model only when Lingua returns
  `uncertain`.
- If GlotLID also returns `uncertain`, the tool keeps the original Lingua
  result.
- A row that the fallback resolves has `reason=fallback_glotlid_v3`. All other
  reasons keep their normal meaning.

You can see one detail of the fallback arithmetic in the data. fastText
accumulates its softmax in float32. Thus a confident prediction can be slightly
over 1.0. The value 1.0000100135803223 occurred in 12 of 194 Afghan
descriptions. A probability above one is a rounding effect. It is not a score.
The tool clamps the values within 1e-4 of 1.0 to exactly 1.0. It still refuses
any value beyond that. The tool never changes a score inside the interval.

The fallback artifact is `cis-lmu/glotlid`, file `model_v3.bin`, revision
`85cd6716494360367b75f642b5bc78667605d0b4`, with SHA-256
`a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e`. Its Linux
runtime is pinned to `fasttext-numpy2==0.10.2`. The snapshot records this
identity. Thus a run cannot change the model without a message.

## Sentence splitting

The splitting runs in the same pass as the detection. It is not a second sweep.
The cold start is the main cost of a Grid'5000 run. 386 one-shard jobs pay about
6.4 hours of repeated model loading. The warm inference takes about 49 minutes.
A separate stage would almost double the cost. It would compute again something
that is already in memory. The splitting is a pure function of the text and the
detection result.

The splitter is SaT-3l-sm (`segment-any-text/sat-3l-sm`, revision
`137da054051ad9f1eac42025f758db4ac9f22535`, `model.safetensors` with SHA-256
`3e19cb0e5dbe9790d37d918d7e87880cb6577d497833f0f0627d80ae6ca1fe90`). It runs
through `wtpsplit==2.2.1`.

**The tool splits a description only when it detected the language *and* the
team trained the splitter on that language.** SaT is language-agnostic at
inference. It takes no language argument. In the API of the library,
`lang_code` selects a style adapter that this configuration does not use. The
85 languages of its supervised mixture are the languages where it is *known to
be competent*. That list is the gate. The source pins the list. The tool does
not read it from the installed library. The digest of the list is part of
`model_config_fingerprint`. Thus, if you make the list wider or narrower, the
run identity changes. The dataset does not change without a message.

The detection reports ISO 639-3. SaT names its languages in ISO 639-1 (except
Cebuano, which has no 639-1 code). An explicit table joins the two. The table
also maps the macrolanguage members that a pinned detector emits: `nob` and
`nno` to `no`, `arb` to `ar`, `cmn` to `zh`. The tool treats a code that the
table does not cover as unsupported. There is no fallback and no guessing.

Each row has one of three outcomes:

| `split_status` | When | `sentences` |
| --- | --- | --- |
| `split` | Detected, and SaT was trained on that language | The sentences. There can be none |
| `unsupported_language` | Detected, but outside the 85 languages of SaT | Empty |
| `not_detected` | `uncertain` or `non_linguistic` | Empty |

`split_reason` names the specific case: `split_sat_3l_sm`,
`unsupported_language_<iso 639-3>`, or `not_detected_<detection status>`. A
skipped description is still a complete row. It never makes a run incomplete.

Croatian is the clearest example of the gate at work. Lingua detects it with
confidence, and the team never trained SaT on it. Thus the tool publishes these
descriptions unsplit with `unsupported_language_hrv`.

## Limitations

- `top_score`, `runner_up_score`, and `margin` are **raw detector scores. They
  are not calibrated probabilities**. Do not read them as confidence
  percentages.
- **Nobody measured the accuracy on this dataset.** The team chose the detector
  for its documented suitability on short text. The team did not benchmark it
  here. Treat each label as an unvalidated annotation.
- **The cascade does not gate the confidence.** The team removed the minimum
  score and the minimum margin. The tool labels a value when it is long enough,
  when the top two candidates are not an outright tie, and when there is no
  evidence of mixed language. Thus low-confidence labels are expected. The tool
  records `top_score`, `runner_up_score`, and `margin` on each row. Consumers
  can apply their own threshold.
- Short values are still `uncertain` by design. The tool applies a minimum
  letter count before it tries the detection at all.
- When the tool identifies evidence of mixed language, the result is
  `uncertain` with the reason `mixed_text`. The detector can miss mixed-language
  text, especially in short values.
- The detector cannot identify the languages outside its supported set
  reliably. The tool can mark them `uncertain`. It can also assign a supported
  language incorrectly.

## Local setup

The detector is an optional extra. Thus the core dataset build does not need it:

```bash
uv sync --frozen --extra language
```

## Preparation versus execution

The pipeline separates three tasks: freeze the inputs, use a processing budget,
and audit the result. You can repeat each task independently.

### 1. Freeze the input snapshot

```bash
uv run osm-polygon-description-tag language prepare --source-root "/path/to/data-root/data" --run-dir "/path/to/data-root/language-run-lingua-glotlid-v3-full" --project-root .
```

This command writes `snapshot.json`. The snapshot binds the relative path, the
size, the SHA-256, the schema identity, and the row count of each source
Parquet file. It also binds the detector configuration and the fingerprints of
the code and of `uv.lock`.

The snapshot identity **excludes the absolute source root** on purpose. Thus,
if you stage a shard on another machine, the identity does not change. If you
run `prepare` again with unchanged inputs, nothing changes. If a source file
changed, the command fails. It does not rewrite the identity without a message.

### 2. Process one shard

```bash
uv run osm-polygon-description-tag language run \
  --source-root <staged-source> --run-dir <run-dir> --project-root <staged-project> \
  --shard region.parquet --batch-size 512 --budget-seconds 1200 \
  --glotlid-model-path <model_v3.bin> \
  --sat-model-path <sat-3l-sm-dir>
```

`run` needs only the file of the selected shard. It does not need the whole
dataset. It streams the projected Arrow batches (`source_pbf`, `osm_type`,
`osm_id`, `tags`). It never loads a whole file or the whole dataset. The batch
processing and the cache for repeated text are bounded. The exact duplicate
detection still keeps the description identities. The memory grows with the
number of annotations in the shard (or in the selected run during a full
validation). It does not stay constant for very large inputs. Choose small
shards. The batch-size setting alone does not bound this identity bookkeeping.
Before the inference, the current project source and the lockfile must match the
frozen fingerprints. The tool builds the detector with the language scope of the
snapshot. It also checks the configuration fingerprint. `--project-root` is the
current working directory by default.

The tool locks the run directory for the attempt. It refuses a second local
worker. Thus the commits cannot interleave.

### 3. Audit what the run produced

```bash
uv run osm-polygon-description-tag language validate --run-dir <run-dir>
```

`validate` is strictly read-only. It never repairs, deletes, or rewrites
anything. It reports what is on disk. You make the decision.

## Exact resume and corruption behaviour

The tool commits each batch in a fixed order: the annotation part, then its
receipt, then the checkpoint. Each write does an fsync of its contents, renames
the file atomically, and then does an fsync of the directory.

**The checkpoint is the single source of truth.**

| Interruption point | Behaviour on the next run |
| --- | --- |
| Before the tool commits the part | The tool processes the batch again from the last checkpoint |
| After the part, before the receipt | The tool rewrites the part deterministically. It adopts nothing |
| After the receipt, before the checkpoint | The same. The checkpoint does not list the part, so the tool does it again |
| After the checkpoint | The run resumes from exactly that cursor |

The part names come from the input row offset. Thus a batch that the tool does
again overwrites its own file. It does not create a duplicate. The output of an
uninterrupted run and the output of a paused and resumed run are
byte-identical.

On resume, the worker verifies each part that the checkpoint lists. The file
must exist. Its hash must match its receipt. It must parse as the frozen
annotation schema. It must have the recorded number of rows. The worker refuses
anything else. The tool rejects a checkpoint from a different snapshot,
detector configuration, shard, row count, or batch size. It does not adopt it.

When the processing budget is exhausted, the tool returns a **paused** state.
It never returns a completed state. Real errors propagate. The tool never
converts them into a completed result.

## Grid'5000

!!! warning "Preflight is mandatory before every run"
    The tool never assumes the account entitlement, the current quota, the site
    capacity, or the live scheduler state. You must check them before each
    authorised run. The preflight fails closed on anything that it cannot
    interpret positively.

!!! success "Execution status"
    **The team processed and published the full dataset.** Snapshot
    `9a03d00020191df375c25d3e4fa9b79e28ac9f8d83868d274a508ab954a84e98` ran to
    completion across all 386 shards. It made 919 126 annotations from 906 631
    source rows. It validates as `complete` with no issues. It is published at
    revision `fec858b679f5ee7e87f0ecfaaa6b7223b2a7f5e2` of
    `NoeFlandre/osm-polygon-description-tag`. It has 388 files under
    `language-v1/`. The team verified all of them against the Hub by size and
    SHA-256.

    Eight sites carried the run: `nancy`, `grenoble`, `lille`, `lyon`,
    `nantes`, `sophia`, `toulouse`, and `luxembourg`. Each job used one core and
    a walltime of 30 minutes at most. Each site had one active job throughout.
    The team excluded `rennes` because of the home quota. The full counts, the
    per-language totals, and the publication record are in
    [the rollout status](language-rollout-status.md#executed).

    The team found five problems only when it ran the pipeline. The team fixed
    all of them:

    - The multi-shard driver read `outcome` at the top level of the submit
      payload. The CLI nests it under `result`. Thus the driver reported a job
      that was really queued as an unclean submission.
    - `AutoTokenizer` cannot resolve a tokenizer class from the `xlm-token` model
      type of SaT. The tokenizer needs its own directory with the XLM-R config.
      Without it, `pad_token_id` is `None`, and the inference stops in the middle
      of a batch.
    - The tool writes the submission intent on the copy of the run on the
      *site*. The tool must adopt it back before it can acknowledge a shard.
    - fastText accumulates its softmax in float32. It returns probabilities
      slightly over 1.0. The score guard refused them.
    - The sites do not agree on the meaning of a bare `oarsub`. Several sites
      select a queue automatically. The queue does not exist, and they reject the
      job. `sophia` refuses an explicit queue. Thus the queue is an input for
      each site.

    Two operational traps are important. First, a driver can stop between the
    end of a job and the retrieval of its results. Then a terminal,
    unacknowledged intent stays on the site. `submit` then correctly refuses to
    retry. You must reconcile the shard with `grid status --apply` and collect
    it. Do not submit it again. Second, the master run has a placeholder
    checkpoint (`paused`, cursor 0) for each staged shard. A merge with
    `rsync --ignore-existing` keeps the placeholder. It drops the real result
    without a message.

    The NumPy baseline blocker that this page describes below is resolved. Each
    frontend reports zero x86-64-v2 flags. It is enough to pin the *operator*
    environment to `numpy==1.26.4`. The inference uses the locked environment
    that the tool builds inside the job.

    The production data stays under the requested Seagate project root. The data
    gets there only when you run the workflow with those paths.

### Job shape

| Constraint | Value |
| --- | --- |
| Cores | exactly 1 (not an exclusive node) |
| Walltime | 30 minutes at most |
| Useful processing | 20 minutes at most. This leaves a margin for setup and termination |
| Concurrency | 1 |
| Staging | one shard |
| GPU, job arrays, speculative submission, automatic resubmission | none |

The dependency installation and the inference both happen **inside the job**,
on allocated compute resources. Use the frontends only for light file
management and scheduler operations.

### Policy safeguards

The preflight fails closed. The public Grid'5000 documentation does not define a
stable `usagepolicycheck` JSON schema. It does not define an exit-code contract
that means "your quota permits this submission". Thus:

- **The tool does not treat exit status zero as approval.** It reads only the
  fields that a real capture showed (`start_time`, `stop_time`, `jobs`,
  `total_jobs`, `limits`). The tool does not invent a "remaining quota" field or
  an "approved" field.
- The tool changes anything that it cannot interpret positively to `unknown`.
  It refuses a submission under `unknown`.
- The tool counts the active jobs from the `oarstat -u -J` JSON job map of the
  selected site. It counts them separately from the historical usage totals.
  This is not a cross-site inventory. During the operator preflight, also check
  the reservations at other sites. Unknown job states do not count as an empty
  account. The command adapter accepts the successful zero-byte no-jobs response
  of OAR 2.5.9, 2.5.10, and 2.6.1. It also accepts the `{}` response of OAR3. It
  does not interpret blank output from failed commands as evidence. It rejects
  the `null` response of the older OAR 2.5.8. Unnamed jobs stay in the active
  count. The tool ignores them when it resolves a particular job name. These
  contracts follow the upstream
  [OAR command implementation](https://github.com/oar-team/oar/blob/debian-upstream/2.5.10/sources/core/qfunctions/oarstat)
  and [OAR3 implementation](https://github.com/oar-team/oar3/blob/master/oar/cli/oarstat.py).
- The tool reads the home quota from `quota -p -w`. It does not use `quota -l`
  on purpose, because `quota -l` excludes NFS home storage. The tool checks the
  block limits and the file-count limits. This includes the raw grace fields and
  the NFS device paths. Truncated home rows make the evidence unknown. The
  parser follows the upstream
  [quota-tools output format](https://kernel.googlesource.com/pub/scm/utils/quota/quota-tools/+/refs/tags/v4.07/quota.c).
- The tool **blocks by default** the weekday daytime in Europe/Paris
  (09:00–19:00). The public documentation does not let you verify the daytime
  accounting. This is a conservative default. It is not a permanent restriction.
  After you confirm your own accounting, give `--allow-daytime`. The daytime
  quota is not a universal "two core-hours" allowance. Short jobs are not
  automatically exempt. Do not assume besteffort privileges.
- The tool requests `night=noretry`. Thus the scheduler does not retry a
  postponed night job without a message.
- The entire requested walltime must fit in one day/night window. A job that
  ends exactly at the boundary is allowed. The tool refuses a job that crosses
  it.

### Workflow

Run the scheduler commands and the transfer commands from the frontend of the
chosen site. Use absolute paths in storage that the allocated compute job can
see. The transfer plans use filesystem paths. They do not use an SSH hostname.
Thus they do not establish a Mac-to-Grid connection. Arrange the authorised
transfer to that site separately. Do not use SSH to connect directly to a shared
compute node. Follow the OAR access rules of the site for a one-core
reservation.

The Python environment of the operator must be already installed. Prepare it on
allocated compute resources. Do not compile dependencies on a frontend. The
examples use `uv run --no-sync` to prevent an implicit installation during
frontend operations. The generated job needs `bash` and `uv` on the compute
node. The transfers need `rsync`. The scheduler operations need the OAR tools
and the quota tools of the site. Check these prerequisites before an authorised
run.

!!! warning "Build the operator environment for the CPU of the frontend"
    An environment that you build on an allocated compute node can be unusable
    on the frontend that must run `oarsub`. A `nancy` operator environment that
    was built this way failed on the frontend with `NumPy was built with
    baseline optimizations: (X86_V2) but your machine doesn't support:
    (X86_V2)`. This breaks each `grid` subcommand before it reaches the
    scheduler. Build the operator environment on a machine with a CPU baseline
    that the frontend also satisfies. Before you stage anything, check it with a
    harmless `osm-polygon-description-tag --help`.

    You can measure the cause. `fnancy` reports a `Common KVM processor`. The
    `/proc/cpuinfo` flags contain `pni` and `cx16`. They do not contain `sse4_2`
    or `popcnt`. Thus it is an x86-64 baseline machine with SSE3. The NumPy 2
    wheels need x86-64-v2 and stop on import. The NumPy 1.26 wheels have an SSE3
    baseline and run. It is enough to pin the *operator* environment to
    `numpy==1.26.4`. This does not change the model semantics. The job script
    runs `uv sync --frozen --no-dev --extra language` on the allocated compute
    node. Thus the inference always uses the locked environment. Before you
    depend on this, verify that the split holds:

    ```bash
    ssh nancy 'grep -m1 flags /proc/cpuinfo | tr " " "\n" | grep -cE "^(sse4_2|popcnt)$"'
    # must print 0, which is why NumPy 2 cannot be used on the frontend
    ```

### Drive all 386 shards

`osm-polygon-description-tag language grid run` sequences the per-shard protocol
below. It adds no policy of its own. For each shard, it does these steps:

1. It stages and transfers one shard.
2. It submits the shard through the apply gate of the CLI.
3. It polls until the scheduler reports a terminal state.
4. It collects and acknowledges the shard.
5. Then it goes to the next shard.

If a result is ambiguous, active, or unresolved, the run stops. An operator must
reconcile it. The driver forwards `--allow-daytime` only when you give it
explicitly.

The driver exists because `grid stage --apply` emits a *filesystem* rsync argv.
It cannot reach the site from a workstation. The driver does that transfer over
SSH. This is the separately arranged authorised transfer that this runbook
requires.

```bash
uv run osm-polygon-description-tag language grid run \
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

Start with `--max-shards 1`. Read the emitted JSON before you make it larger.
You can resume the driver. It skips a shard when its checkpoint already
validates as complete. Thus a new run continues. It does not repeat work.

### Stage the pinned models one time

The cascade needs the pinned fallback model on storage that the compute node can
read. `--glotlid-model-path` is an absolute path. The job verifies its SHA-256
before it loads the model. Fetch the model one time into the shared home. Check
the digest against the pinned constant:

```bash
mkdir -p ~/models/glotlid-v3
curl -sSL -o ~/models/glotlid-v3/model_v3.bin \
  "https://huggingface.co/cis-lmu/glotlid/resolve/85cd6716494360367b75f642b5bc78667605d0b4/model_v3.bin"
sha256sum ~/models/glotlid-v3/model_v3.bin
# must print a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e
```

The artifact is about 1.6 GiB. Before you fetch it, confirm that `quota -p -w`
shows enough space.

WARNING: Never use a digest that is not the same as the pinned constant. The
loader refuses it. You must also refuse it.

The sentence splitter needs the same procedure, with one difference.
`--sat-model-path` is a **directory**. It is not a file. `wtpsplit` loads a
model in the same way as `transformers`. It loads it from a directory that has
`config.json` next to the weights. It also needs a tokenizer in the staging.
The default of the library fetches `xlm-roberta-base` from the Hub. A compute
node has no reason to have network access. The job verifies the SHA-256 of the
weights before it loads anything.

Put the tokenizer in its own `tokenizer/` subdirectory. Put the *own*
`config.json` of XLM-R next to it. This is not only for order. The `config.json`
of SaT declares the custom model type `xlm-token`. No tokenizer class is
registered for it. Thus a tokenizer that the tool loads from the model
directory becomes a generic fast tokenizer with no special tokens. Then
`pad_token_id` is `None`. The padding writes `None` into the input ids. The
inference stops in the first batch with a `TypeError` from deep inside
`wtpsplit`. The config of XLM-R names the real tokenizer class. This makes
`<pad>` resolve to id 1. This is exactly the `pad_token_id` that the own config
of SaT expects.

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

The weights are about 815 MiB. The tokenizer adds about 17 MiB. Plan for about
2.5 GiB of home quota for the two models together. Before you submit 386 jobs,
confirm that the directory loads:

```bash
python -c "from wtpsplit import SaT; d='$HOME/models/sat-3l-sm'; \
  print(SaT(d, tokenizer_name_or_path=d + '/tokenizer').split('A park. It has benches.'))"
```

Prepare a portable payload. It contains the project, the lockfile, the
immutable snapshot, exactly one source shard, and the validated resume
artifacts:

```bash
uv run --no-sync osm-polygon-description-tag language grid stage --run-dir <run-dir> --project-root <project> --source-root <source> --shard region.parquet --remote-bundle-dir /home/user/language-bundle \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin \
  --sat-model-path /home/user/models/sat-3l-sm
```

This command creates local staging files. It prints the exact transfer argument
vector. It does not transfer or submit anything. Add `--apply` only when the
printed source and destination are correct and visible in the current
filesystem. After the collection, stage again to include the newly committed
checkpoint. Do this before an explicitly requested continuation.

```bash
uv run --no-sync osm-polygon-description-tag language grid prepare --run-dir <run-dir> --shard region.parquet --remote-project-dir /home/user/project --remote-source-dir /tmp/staging/source --remote-run-dir /tmp/staging/run \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin \
  --sat-model-path /home/user/models/sat-3l-sm
```

`prepare` is the lower-level script and metadata operation for inputs that are
already staged. Unlike `stage`, it does not copy the project or the source data.
It binds the code fingerprint, the lock fingerprint, the snapshot, and the
selected shard to the job script. After the staging, nobody can rewrite the
prepared settings without a message. The tool invokes the scheduler with an
argument vector. The tool shell-quotes the final script argument separately,
because OAR later evaluates that stored command through the shell of the user.
Thus spaces and metacharacters in a local script path stay part of the
filename. They are not executable syntax.

```bash
uv run --no-sync osm-polygon-description-tag language grid submit --run-dir <run-dir> --shard region.parquet --site nancy --remote-project-dir ... --remote-source-dir ... --remote-run-dir ... --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin --sat-model-path /home/user/models/sat-3l-sm
```

Without `--apply`, this command only makes a plan. It does not contact a
scheduler. It prints the exact `oarsub` argument vector and the policy verdict.
With `--apply`, it gathers live policy evidence and submits.

**The tool writes the submission intent durably before `oarsub` runs.** The call
can time out, or it can answer without a job identifier. Then the outcome is
`ambiguous`, and the intent on disk proves that a job can already exist. Never
resolve an ambiguity by submitting again.

```bash
uv run --no-sync osm-polygon-description-tag language grid status --run-dir <run-dir> --shard region.parquet --apply
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run-dir> --shard region.parquet
```

`status` reconciles a recorded submission before it does anything else.
`collect` validates the returned checkpoints and parts.

To retrieve a completed attempt into a separate local staging directory, first
inspect the plan without `--apply`:

```bash
uv run --no-sync osm-polygon-description-tag language grid collect --run-dir <run-dir> --shard region.parquet --remote-bundle-dir /home/user/language-bundle --retrieved-run-dir <retrieval-dir>
```

With `--apply`, the command retrieves only the state of the selected shard. It
validates the state against the immutable snapshot. It imports the committed
artifacts and records the collection acknowledgment. A paused checkpoint is not
a completion. A later attempt needs a terminal scheduler reconciliation and a
validated collection. Never bypass an active or ambiguous attempt by submitting
again. The import holds the submission lock and the worker lock. It validates a
private copy. It refuses backward progress. It refuses changes to the history
that is already committed. Then it commits the new parts and receipts before the
checkpoint. An interruption leaves the old checkpoint authoritative. Retry the
collection with the same retrieved state.

Put the durable validated results on the external project storage. Never fall
back to internal storage if that mount is not available.

### Crash recovery

`prepare` and `stage` write a validated zero-cursor checkpoint for the shard
before a job can be submitted. The checkpoint is `paused` for a shard with input
rows. It is `complete` for a zero-row shard. They take the run-wide submission
lock first and the exclusive worker lock second. They always use this order. The
commands preserve an existing valid checkpoint. They never rewrite it. They fail
closed on a corrupted, symlinked, or conflicting state, or on any other
unexpected state. A shard whose checkpoint cannot be initialised cannot be
submitted. A job can die during setup, before it processes one row. It still
leaves a checkpoint that `collect` can validate and acknowledge. This makes one
bounded retry safe. The tool never resubmits an active or ambiguous submission.
You must reconcile it.

A worker can die after it writes a part or a receipt but before it commits its
checkpoint. Then it leaves artifacts that the authoritative checkpoint does not
list. The tool never adopts them and never deletes them. `stage` moves the
recognised generated orphans into `shards/<key>/quarantine/`. It reports them in
its JSON output as `quarantined`. Then it stages the state that the checkpoint
lists again, so that the shard can be collected and resumed. `stage` refuses
anything that is not a recognised generated artifact. Examples are an unknown
filename, a symlink, and a FIFO. It does not move them. It never touches the
committed parts and receipts. The public `validate_run` contract does not
change. It still reports each artifact that a checkpoint does not account for as
an issue. It does not ignore it. The commit order stays: parts, then receipts,
then the checkpoint last.

## Publication

This implementation did not do a Hugging Face upload. The team tested the
commands below only against local fakes.

The publication is additive. The data uploads are allowlisted under
`language-v1/`. The same atomic Hub commit adds the corresponding configuration
and the generated section to the root `README.md`. It preserves the existing
configurations, the default selection, and the unrelated prose. Nothing deletes
remote files. The installed training configuration lists exactly the planned
Parquet paths. It does not use a wildcard that can include stale or unrelated
files by accident. The tool refuses an existing conflicting language
configuration. It does not change it.

```bash
uv run osm-polygon-description-tag language export --run-dir <run-dir> --export-dir <export-dir> --card-section <path>.md
```

`export` refuses to run unless **each** snapshot shard validates as complete.
Thus a partial run cannot be published as if it covered the dataset. It writes
`language-v1/data/*.parquet`, `language-v1/stats.json`, and optionally the
generated dataset-card section. All counts come from the exported rows. The
final `language-v1/export-manifest.json` binds the data and the statistics by
size and SHA-256. The tool refuses missing manifests and changed files. This
includes an interrupted re-export that leaves files from different attempts. A
re-export marks this manifest as in-progress before it touches data. Only a
successful retry restores a completed seal. The export and the publication share
a local lock. The publication checks the sealed files again after it acquires
the lock and before it contacts the Hub.

```bash
uv run osm-polygon-description-tag language publish --export-dir <export-dir> --repo NoeFlandre/osm-polygon-description-tag --confirm-repo NoeFlandre/osm-polygon-description-tag
```

Three gates control the publication. You must repeat the repository identifier.
You must give `--apply`. `--baseline-revision` must match the current revision
of the repository. If the repository moved, the tool reports `drifted` and
refuses to continue. It does not overwrite. A plan, including a drift refusal,
never writes publication state.

After the upload, the tool verifies each planned file against the Hub by size
and SHA-256 at the exact resulting revision. It also checks the Dataset Viewer
for the `language-v1` configuration. If the tool cannot establish the success of
an upload, it records the upload as `ambiguous`. The next invocation **verifies**
the upload. It does not upload again. The tool persists the intent **before** the
upload starts. This includes library calls that omit an explicit state path. The
tool checks the publications that it verified before again. Cached state cannot
hide a later remote file loss. The tool refuses an unresolved state that belongs
to a different plan.

The adapter reads the existing card at the baseline revision. It creates the
data-and-card commit with that revision as its optimistic concurrency guard. The
adapter refuses malformed or conflicting card configurations.

WARNING: Do not replace the existing `configs` list with a list that has only
the language configuration. This hides the default dataset.

The optional `--card-section` file is a local preview. It is not a separate
manual publication step.

The Dataset Viewer is asynchronous. Its `/splits` endpoint has no version. The
card metadata alone does not prove that `language-v1/train` is queryable. If the
indexing is pending or failed, the publication stays unverified. Run the
verification again later. Do not repeat the upload. A matching repository head
and a ready Viewer response are useful readiness evidence. They are not proof
that the Viewer indexed an exact commit.

## Reproducibility and provenance

### Quality-gate interpretation

The mutation reports count generated mutants. They do not count mathematically
equivalent program variants. As in the rest of this repository, the team marks
some serialization statements and static-typing statements narrowly and
excludes them. Mutmut cannot distinguish an equivalent argument change there.
The cases are: `ensure_ascii=False` versus `None`, the unused JSON object-key
separator in a flat identity array, and the type argument of `typing.cast`. The
LRU eviction call is also marked narrowly. `OrderedDict.popitem(last=None)` has
the same behavior as `last=False`. The tests check the recency and the bounded
eviction explicitly. These exclusions are visible in the source. Behavioral
tests still cover the exact Unicode bytes, the identity hashes, and the decoded
values. The team never counts a survivor, a timeout, or an unchecked generated
mutant as killed.

### Run identity

Each annotation row records the `snapshot_id` and the `model_config_fingerprint`
that produced it. The snapshot records the source file hashes, the schema
identity, the detector policy, and the fingerprints of the code and the
lockfile.

The tool pins Lingua and its version. It verifies them at construction time. For
the cascade, the configuration fingerprint also records the exact GlotLID
repository, revision, runtime, and model SHA-256. The tool populates
`binary_artifact_hash` only with that independently verified pinned artifact.
Legacy pure-Lingua snapshots can leave it unset.

The snapshot also records the splitter by name: `splitter_name`,
`splitter_revision`, and `splitter_languages_fingerprint`. The tool binds these
fields into `snapshot_id` through `config_fingerprint` in any case. But nobody
can read a fingerprint. A person who opens `snapshot.json` must be able to say
which splitter produced the sentences without a new hash computation. The tool
verifies the fields whenever they are present. A snapshot that the tool wrote
before the fields existed has nothing there to disagree with. Thus parsing it
still works. But its `snapshot_id` was hashed over a payload without the fields.
It no longer verifies. You must prepare again a run directory that the tool
froze before this change. Do not resume it.

The detection is deterministic. Scores within the fixed tie epsilon produce an
uncertain result without a language label. Thus the order that the provider
gives for tied languages cannot change the annotation.

## Licensing and attribution

The annotated text is OpenStreetMap data, © OpenStreetMap contributors. The
Open Database License (ODbL) applies to it. The language labels are derived
annotations. The tool produces them with the pinned `lingua-language-detector`
primary and the documented GlotLID v3 fallback. The dataset card records both
upstream attributions and the exact model provenance.
