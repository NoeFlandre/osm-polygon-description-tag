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
| `top_score` | float64 (nullable) | **Raw** detector score |
| `runner_up_score` | float64 (nullable) | **Raw** runner-up score |
| `margin` | float64 (nullable) | `top_score - runner_up_score` |
| `status` | string | `detected`, `uncertain`, or `non_linguistic` |
| `reason` | string | Why that status was assigned |
| `snapshot_id` | string | The immutable input snapshot this row came from |
| `model_config_fingerprint` | string | Detector library, version, scope, and policy |

## Detector pipeline

The production detector is a deterministic cascade:

- Lingua 2.2.0 is the primary detector and applies the conservative policy.
- The pinned GlotLID v3 model is called only when Lingua returns `uncertain`.
- If GlotLID also returns `uncertain`, the original Lingua result is retained.
- A fallback-resolved row has `reason=fallback_glotlid_v3`; all other reasons
  retain their normal meaning.

The fallback artifact is `cis-lmu/glotlid`, file `model_v3.bin`, revision
`85cd6716494360367b75f642b5bc78667605d0b4`, with SHA-256
`a818b6bd42a628ab47d3dfc1578c7ea615c45381f3494c42535e31e8c4cafc9e`. Its
Linux runtime is pinned to `fasttext-numpy2==0.10.2`. The snapshot records this
identity, so a run cannot silently switch models.

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
  --glotlid-model-path <model_v3.bin>
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

!!! danger "Execution status"
    **The full dataset has not been processed and nothing has been published
    to Hugging Face.** What has actually run on Grid'5000 is a three-shard
    pilot on site `nancy` under the V2 policy: OAR jobs `6917617`
    (`afghanistan-latest.parquet`), `6917620` (`albania-latest.parquet`), and
    `6917621` (`algeria-latest.parquet`), each one core with a 1800 s
    walltime, all reconciled to `terminated`, collected, and acknowledged on
    2026-09-09. That pilot covered 2 267 of 906 631 snapshot rows (0.25 %).

    Those pilot results are **not** publishable: `snapshot_id` binds the code
    and lockfile fingerprints, and both have since changed, so a full run
    starts from a fresh snapshot. Publication additionally refuses any
    incomplete run, so the Hugging Face step stays blocked until all 386
    shards are complete under one snapshot.

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

Prepare a portable payload containing the project, lockfile, immutable
snapshot, exactly one source shard, and validated resume artifacts:

```bash
uv run --no-sync osm-polygon-description-tag language grid stage --run-dir <run-dir> --project-root <project> --source-root <source> --shard region.parquet --remote-bundle-dir /home/user/language-bundle \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin
```

This creates local staging files and prints the exact transfer argument vector.
It neither transfers nor submits anything. Add `--apply` only when the printed
source and destination are correct and visible in the current filesystem.
After collection, stage again to include the newly committed checkpoint before
an explicitly requested continuation.

```bash
uv run --no-sync osm-polygon-description-tag language grid prepare --run-dir <run-dir> --shard region.parquet --remote-project-dir /home/user/project --remote-source-dir /tmp/staging/source --remote-run-dir /tmp/staging/run \
  --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin
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
uv run --no-sync osm-polygon-description-tag language grid submit --run-dir <run-dir> --shard region.parquet --site nancy --remote-project-dir ... --remote-source-dir ... --remote-run-dir ... --glotlid-model-path /home/user/models/glotlid-v3/model_v3.bin
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

Detection is deterministic: scores within the fixed tie epsilon produce an
uncertain result without a language label, so the provider's ordering of tied
languages cannot change the annotation.

## Licensing and attribution

The annotated text is OpenStreetMap data, © OpenStreetMap contributors,
available under the Open Database License (ODbL). Language labels are derived
annotations produced with the pinned `lingua-language-detector` primary and the
documented GlotLID v3 fallback. The dataset card records both upstream
attributions and exact model provenance.
