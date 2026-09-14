# Takeover brief: finish the language+sentence run on Grid'5000 and publish it

You are taking over a nearly-complete production run. Read this whole file
before running anything. Numbers marked **verify** are the ones you must
re-derive rather than trust.

## 1. Environment and hard constraints

- Work only in `/Volumes/Seagate M3/projects/osm-polygon-description-tag`, on
  branch `main`. This is an external HDD ("the volume"); it is slow for
  small-file reads (~30 ms each). All data and all run state live here and
  must stay here.
- **Keep the internal SSD (`/private/tmp`) free.** Scratch there is disposable
  and must be deleted when done. Budget under ~1 GiB transient. The user has
  asked for this explicitly, twice.
- The user runs **other projects on this machine and on Grid'5000**. At the
  time of writing an `lrb-publish` job of theirs was running on site `nancy`.
  Never cancel a job you did not submit. Never assume an idle CPU.
- Respect Grid'5000 usage policy: 1 core, walltime <= 30 min, **one active job
  per site**, no GPUs, no job arrays, no speculative submission. The policy
  gate in this codebase enforces that and fails closed; do not weaken it.
- Do not use `git reset --hard`, `git clean`, or broad stashes. There are
  uncommitted changes that matter (see section 6).

## 2. What the work is

`language-v1` annotates every `description` / `description:<suffix>` tag value
in 386 source Parquet shards (906 631 rows) with a detected language and, when
the detected language is one SaT-3l-sm was trained on, a sentence split.
Detection and splitting run in the same pass on a compute node.

## 3. State at handover

- Snapshot: **`9a03d00020191df375c25d3e4fa9b79e28ac9f8d83868d274a508ab954a84e98`**,
  frozen over all 386 shards. Every run directory must carry this exact id;
  the merge script asserts it.
- Master run directory: `data-root/language-run-sat-full`.
- Backup already taken: `data-root/backups/language-run-sat-20260913-175755`.
- **verify** roughly 382/386 shards complete, ~900k annotations. Get the truth
  with the command in section 4 — do not trust this number.
- Known-remaining shards at handover: `vietnam`, `seychelles`,
  `us-new-jersey`, `us-new-york` (all `-latest.parquet`). `belgium` may have
  completed; re-validate.
- Grid'5000 job `3099464` on `grenoble` was running `vietnam-latest.parquet`.
  Check it before resubmitting — if it reached `Terminated`, **collect** it,
  do not resubmit.

Eight sites are staged and working: `nancy`, `grenoble`, `lille`, `lyon`,
`nantes`, `sophia`, `toulouse`, `luxembourg`. Each has `~/.local/bin/uv`, the
current source at `~/osm-language-grid/project`, an operator venv at
`~/osm-language-grid/.portable-grid-probe` pinned to `numpy==1.26.4`, and both
pinned models under `~/models/`. `rennes` is excluded (home quota ~24.8 of
25 GiB).

## 4. How to see the truth

```bash
cd "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
export PATH="/opt/homebrew/bin:$PATH" TMPDIR=/private/tmp/osm-pdt-work
bash /private/tmp/osm-pdt-work/merge_runs.sh          # union results into master
.venv/bin/osm-polygon-description-tag language validate \
  --run-dir data-root/language-run-sat-full
```

`merge_runs.sh` merges **`shards/` only** from every
`language-run-sat-*`, `language-run-sweep-*`, `language-run-one*` directory
into the master, and refuses if any snapshot id differs. It deliberately does
**not** merge `jobs/`: an unresolved submission intent copied into the master
would make the master refuse to ever resubmit that shard.

Beware: this full validate takes minutes (section 5, trap 6).

## 5. Traps that have already cost hours. Do not rediscover these.

1. **`uv run` deadlocks the driver.** `uv run` holds a lock on the shared uv
   cache for the whole command. The driver calls the CLI once per shard, so a
   process holding the lock spawned a child waiting for it. Seven parallel
   drivers wedged for 30 minutes with no child process at all. `_cli()` in
   `scripts/run_language_grid.py` now returns the console script beside
   `sys.executable`. **Never put `uv run` back in that path**, and invoke the
   driver as `.venv/bin/python -m scripts.run_language_grid`.

2. **`shard_is_complete` validates the entire run directory.** One call
   measured **3 m 40 s** (0.63 s CPU, rest blocked) against a run directory
   holding 384 shards. The driver's per-shard loop is therefore quadratic:
   ~55 checks per site ≈ 3.4 hours *just to find the work*. For a handful of
   named shards, bypass discovery entirely — use
   `/private/tmp/osm-pdt-work/one_shard.py`, which does stage → submit → poll
   → collect for named shards against a **fresh, empty** run directory
   (nothing to scan). Fixing this properly (making validate O(1) per shard)
   is the single highest-value improvement to this tooling.

3. **Sites disagree about the scheduler queue.** `nancy`, `grenoble`, `lille`,
   `lyon`, `nantes`, `toulouse`, `luxembourg` need `--queue default`; a bare
   submission auto-selects a queue named `abaca` that does not exist and is
   rejected. `sophia` **refuses** an explicit queue and must be submitted bare.

4. **Killing a driver mid-job strands the shard.** The submission intent is
   written on the *site's* copy of the run, not locally. A terminal,
   unacknowledged intent makes `submit` refuse with *"terminal job results must
   be collected and acknowledged before retrying"* — correct, but it blocks
   that shard forever. Recovery: either collect it (`grid status --apply` on
   the frontend, then rsync the remote run dir back and `grid collect --apply`),
   or `rm -rf ~/osm-language-grid-sat/<shard-slug>` on that site so staging
   starts clean. The slug is the shard name with every non-alphanumeric
   character replaced by `-`.

5. **Background processes launched from inside a Monitor die with it.** Launch
   long-running work from a plain shell with `nohup`, never from a monitor
   whose script then exits.

6. **Quoting.** A dispatcher bug passed two shard names as one argument
   (`--shard "a.parquet b.parquet"`), so neither ran. Check any generated
   command line before trusting it.

7. **Seeding clones cuts both ways.** Copying completed `shards/` into a new
   run directory stops duplicate compute, but it is exactly what makes trap 2
   expensive. For single-shard work, use empty run directories.

## 6. Code state — uncommitted, all of it needed

`git status` shows substantial uncommitted work. It is green and must be
committed (section 8). Notable pieces, all added this session:

- `--queue` threaded through `grid_scheduler` → `grid_operator` →
  `language_cli` → `scripts/run_language_grid.py`.
- `adopt_retrieved_intent` in `grid_operator.py`: brings the site's submission
  intent into the owned run so a shard can be acknowledged at all.
- `_cli()` no longer uses `uv` (trap 1).
- `selected_shards` (`--shard-stride` / `--shard-index`) for partitioning.
- Snapshot payload now records `splitter_name`, `splitter_revision`,
  `splitter_languages_fingerprint`, verified only when present so older
  payloads still parse.
- GlotLID scores within `1e-4` of 1.0 are clamped: fastText really returns
  1.0000100135803223 (float32 softmax), seen on 12 of 194 Afghan descriptions.
- SaT tokenizer lives in `<model-dir>/tokenizer` with XLM-R's own `config.json`;
  without it `AutoTokenizer` cannot resolve the class from SaT's `xlm-token`
  model type, `pad_token_id` is `None`, and inference dies mid-batch.
- An autouse fixture in `tests/conftest.py` refuses outbound network in tests.
- `--mutation-batch-size` on the mutation gate (default 4 meant ~2 987 mutmut
  invocations, each re-scanning all 87 source files; 400 gives ~52).

**Changing anything under `src/` changes `code_fingerprint` and therefore
`snapshot_id`, invalidating the in-flight run.** `fingerprint_project_source`
covers `src/` only — `scripts/` and `tests/` are safe to edit mid-run.

## 7. Finish the run

For each remaining shard, on a site with no active job and the right queue:

```bash
cd "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
export PATH="/opt/homebrew/bin:$PATH" TMPDIR=/private/tmp/osm-pdt-work
d=data-root/language-run-one-<site>; rm -rf "$d"; mkdir -p "$d"
cp data-root/language-run-sat-full/snapshot.json "$d/snapshot.json"
mkdir -p data-root/language-retrieval-one-<site>
nohup caffeinate -i -s .venv/bin/python /private/tmp/osm-pdt-work/one_shard.py \
  <site> "$d" data-root/language-retrieval-one-<site> <queue|""> \
  <shard>-latest.parquet > /private/tmp/osm-pdt-work/one-<site>.log 2>&1 &
```

One shard per site at a time. If a shard's job already reached `Terminated`,
collect it instead (trap 4). Then merge and validate until `complete: true`
for all 386.

## 8. Quality gates — required before publishing

The user's standing bar, non-negotiable:

```bash
just check      # lock, pre-commit, ruff format+lint, ty, pytest, build
just risk       # CRAP must be < 6 for every function
uv run mkdocs build --strict
```

Mutation gate to **100 %, every unresolved bucket zero**. It is I/O-bound on
the volume, so mirror to the SSD:

```bash
M=/private/tmp/osm-pdt-split
rsync -a --delete --exclude .venv --exclude data-root --exclude mutants \
  --exclude reports --exclude .git --exclude site --exclude dist ./ "$M/"
cd "$M" && uv sync --frozen
export TMPDIR=/private/tmp/osm-pdt-gate MUTATION_TMP_ROOT=/private/tmp/osm-pdt-gate/scratch
COVERAGE_FILE="$M/data-root/.tmp/.coverage-ctx" .venv/bin/python -m pytest -q \
  -p no:cacheprovider --cov=osm_polygon_description_tag --cov-branch \
  --cov-context=test --cov-report=
.venv/bin/python -m scripts.run_mutation_gate --max-children 4 \
  --fast-tests-per-function 1 --mutation-batch-size 400 \
  --coverage-file "$M/data-root/.tmp/.coverage-ctx"
.venv/bin/python scripts/check_mutation_score.py --mutants-root mutants \
  --output reports/mutation-summary.json --minimum-score 100
```

**`MUTATION_TMP_ROOT` is mandatory** — without it the gate's janitor never
runs and it leaked **79 586 pytest directories / 5.0 GB** onto the SSD.
Do not run the gate concurrently with heavy CPU work: a contended run produced
three *false* verdicts (a mutant reported "survived" that its own test kills
outright, and two "segfaults" that fail 19 and 21 tests). **Verify every
surviving mutant individually** before writing a test for it:

```bash
PYTHONPATH="$M/mutants/src" MUTANT_UNDER_TEST="<mutant>" \
  .venv/bin/python -m pytest <its test file> -q --no-cov
```

Delete the mirror and all `/private/tmp/osm-pdt-*` scratch when finished.

TDD applies to any code you write: failing test first, observe red, smallest
change, green. No tautological tests, no tests that mirror the implementation,
no new mutation pragmas without a written line-specific equivalence proof.

## 9. Publish (the user has explicitly authorised this)

```bash
.venv/bin/osm-polygon-description-tag language export \
  --run-dir data-root/language-run-sat-full \
  --export-dir data-root/language-export \
  --card-section data-root/language-card-section.md

.venv/bin/osm-polygon-description-tag language publish \
  --export-dir data-root/language-export \
  --repo NoeFlandre/osm-polygon-description-tag \
  --confirm-repo NoeFlandre/osm-polygon-description-tag      # plan only, no --apply
```

Read the plan. Then add `--apply` and `--baseline-revision <current>`.

`export` refuses unless all 386 shards validate complete — that guard exists so
a partial run cannot be published as though it covered the dataset. **Do not
bypass it.** The user asked about a partial upload earlier and accepted waiting.

Publication is additive: data under `language-v1/`, plus a config entry and a
generated `README.md` section, in one atomic commit. Nothing is deleted. After
upload it re-verifies every file against the Hub by size and SHA-256 at the
resulting revision and checks the Dataset Viewer. An upload whose success
cannot be established is recorded as `ambiguous`; the next invocation
**verifies** rather than re-uploading — never force a re-upload.

These publish commands have **only ever run against local fakes**. Expect the
same class of surprises the grid run produced, and read errors carefully rather
than retrying.

## 10. Finish up

- Update `docs/language-rollout-status.md` and the "Execution status" block in
  `docs/language-detection.md` with what actually happened: real totals, test
  count, coverage, CRAP max, mutation totals, and the Hub revision.
- Commit on `main` with a Conventional Commit message ending
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`, push, and confirm
  `HEAD == origin/main` with a clean worktree.
- Delete `/private/tmp/osm-pdt-*` and confirm the SSD is clear.
- Consider deleting the now-redundant per-site run directories
  (`language-run-sat-*`, `language-run-sweep-*`, `language-run-one*`) once the
  master validates complete and the backup is confirmed — ask the user first.

## 11. Report honestly

State what passed, what failed, and what you did not verify. Do not report a
mutation score from an interrupted run. Do not describe a progress line as a
final verdict. If something is blocked, say so plainly and say why.
