# Development

## Environment

Python 3.12 and uv define the locked environment:

```bash
uv sync --locked
```

The project uses these tools:

- Ruff for formatting and linting
- ty for static typing
- pytest for tests
- pre-commit for local hooks
- Just for named workflows
- Typer for the CLI
- Rich and tqdm for the interactive stderr presentation
- GitHub Actions for CI

## Local gates

```bash
just format
just lint
just typecheck
just test
just test-integration
just build
just risk
just mutation
uv run mkdocs build --strict
just check
```

`just check` runs the locked dependency check, pre-commit, Ruff, ty, the full
pytest coverage gate, and the package build. It never reads the real PBF root.
It never contacts Hugging Face.

`just risk` writes ignored reports under `reports/`. It combines the pytest
coverage with the Radon cyclomatic complexity. It calculates the deterministic
CRAP score (`complexity² × (1 − coverage)³ + complexity`) for each source
function. The budget for the repository is strict: each function must stay
below 6.

`just mutation` checks the generated mutants across all source modules. It
uses a short deterministic triage pass. Then it runs again each survivor,
timeout, and untested mutant against its complete mutmut association. The gate
requires that the tests kill 100% of the generated mutants. No status can stay
unresolved. The documented source-level exclusions are the only exception. The
full regression suite, with the contract tests, is a separate required gate.
The `mutants/` cache of mutmut is ignored. You can reuse it locally. CI runs
both repository-wide gates as required jobs.

### Run the mutation gate on a fast disk

The gate is I/O bound. It is not CPU bound. On the external `Seagate M3` volume
that holds this checkout, a cold read of 400 small source files takes about 12
seconds. This is about 30 ms for each file. The gate reads the source tree
again for each mutant. On the same machine, one full suite run took more than
40 minutes from the volume. It took 152 seconds from the internal disk.

Run the gate from a scratch copy on a fast local disk. Keep the volume as the
source of truth:

1. Copy `src/`, `tests/`, `scripts/`, `pyproject.toml`, and `uv.lock` to a
   scratch directory. In the scratch directory, run `uv sync --frozen
   --all-extras`.
2. Copy `mutants/**/*.py.meta`, `mutants/mutmut-stats.json`, and
   `mutants/mutmut-recorded-tests.json` to the scratch directory. Then the run
   resumes. It does not start from zero. Mutmut generates the mutant sources
   itself.
3. In the scratch directory, run `just mutation-contexts` and the gate.
4. Copy the `.py.meta` files back. Delete the scratch copy.

Keep `TMPDIR` outside the copied tree. Several tests go up from a temporary
path to find `pyproject.toml`. If `TMPDIR` is inside the project, they find the
wrong file. Also give the gate a `TMPDIR` that no other process uses. If the
gate shares a `TMPDIR` with an ad-hoc `pytest` run, the run deletes the
numbered directory under the gate. Then the statistics collection of the gate
stops with `FileNotFoundError`.

Always export `MUTATION_TMP_ROOT` also. The gate starts a janitor that removes
the abandoned scratch directory of each mutant. The janitor starts only when
this variable is set. By default the safeguard is off, and no warning shows. One
interrupted run without the variable left **79 586** pytest directories. They
totalled 5.0 GB.

Increase `--mutation-batch-size` from its default of 4. Each batch is one
`mutmut` invocation. Each invocation scans the whole source tree again before
it runs anything (about 2.3 s). At the default, an 18 000-mutant run pays this
cost about 2 987 times. This is about two hours of pure overhead. At 400, it
pays this cost about 52 times.

WARNING: Do not run the gate beside other heavy CPU work. A contended run
produced three wrong verdicts. One mutant showed `survived`, but its own
existing test kills it. Two mutants showed `segfault`, but they fail 19 and 21
tests.

Confirm each survivor individually before you write a test for it:

```bash
PYTHONPATH=mutants/src MUTANT_UNDER_TEST=<mutant> \
  .venv/bin/python -m pytest <its test file> -q --no-cov
```

### Refresh the test selection of the gate after you add tests

The per-test coverage contexts override the recorded association of mutmut. A
stale context file hides the new tests from the selection. The new tests do not
run, and the mutants that they kill stay as survivors. After you add or rename
tests, do these steps before you run the gate:

1. Run `just mutation-contexts` again.
2. Delete `mutants/mutmut-stats.json` and `mutants/mutmut-recorded-tests.json`.

The rename of a parametrised case is the most important. The stale recording
still has the old test ids. The run then stops in its clean preflight with
`not found:`.

### A pragma works only on a single-line statement

Mutmut records a trailing `# pragma: no mutate` against the line where the
*statement* starts. If you write a pragma against one argument of a multi-line
call, mutmut ignores it without a message. `ruff format` makes this worse. It
moves a trailing comment onto a closing bracket. For a multi-line statement,
use the block form `# pragma: no mutate start` / `end`. The block suppresses
every mutation in the block. Confirm each new pragma. Check that the mutant
that it targets is not generated again.

### Calls to `cast()` are excluded by configuration

`do_not_mutate_patterns` in the `[tool.mutmut]` table skips every expression on
a line that calls `cast(`. Mutating the type argument of `cast()` is equivalent,
so no pragma is needed for it. Mutating the value argument is not equivalent,
and that mutant is hidden as well. Do not add a pragma for a `cast()` call, and
do not widen the pattern to other calls.
`tests/contracts/test_no_mutate_pragma_ratchet.py` enforces both rules and caps
the number of `pragma: no mutate` lines in `src/`.

### A timeout is an unresolved verdict, not a kill

For this gate, a timeout is not a killed mutant. The limit of mutmut is wall
clock: `(estimated_test_time + timeout_constant) * timeout_multiplier`. The
estimate comes from an unloaded baseline run. On a shared machine, this
estimate is too low. Contention alone changes killable mutants into timeouts.
One contended sweep here reported 389 timeouts. Thus `timeout_constant` and
`timeout_multiplier` in `pyproject.toml` are well above the slowest honest run.
If you change one of these values, mutmut discards its cached timeout verdicts
and runs them again. Do this after the machine is quiet again.

## Docker reproducibility

The checked-in `Dockerfile` does these steps:

1. It copies the version-pinned uv binary from `ghcr.io/astral-sh/uv:0.11.16`
   into Python 3.12 Bookworm slim.
2. It installs the runtime graph from `uv.lock`.
3. It includes the `osmium-tool` binary of Debian.

The runtime image contains only the non-editable installed package. It runs as
the unprivileged `app` user. Its default is `--help`, so it cannot start the
pipeline by accident. For an immutable uv provenance, give a verified digest
with `--build-arg UV_IMAGE=...@sha256:...`.

Build the image and run the safe container checks:

```bash
just docker-build
just docker-help
just docker-test
just docker-check
```

The single production command builds or reuses the runtime image. It mounts the
external generated-data root at `/data`. It mounts the `/source` source
directory read-only:

```bash
just docker-run "/path/to/data-root"
```

### Environment that the image uses

| Variable | Default in the image | Purpose |
| --- | --- | --- |
| `OSM_POLYGON_SOURCE_ROOT` | `/source` | Immutable PBF input (mount it read-only) |
| `OSM_POLYGON_DATA_ROOT` | `/data` | Checkpoints, logs, manifests, and artifacts |
| `HF_TOKEN` | unset | Hub publication only. The image never contains it |
| `HF_HUB_OFFLINE` | unset | Set it to `1` to forbid all Hub access |
| `HOME` | `/tmp` | Writable home for caches when the root filesystem is read-only |

`compose.yaml` runs the same image through two services. The `pipeline` service
bind-mounts `$DATA_ROOT` and `$DATA_ROOT/raw` (read-only) as the current user.
The `test` service (`docker compose --profile test run --rm test`) uses the
development target. The image has OCI labels for its source, license, and
title.

The mounted data root keeps the Parquet files, the manifests, the logs, the
caches, and the publication state. `Ctrl-C` is safe. When you run the command
again, it resumes the completed sources. The host passes `HF_TOKEN` only for
the explicit Hub publication step. The Docker build, help, and test commands do
not read real PBF files. They do not contact Hugging Face.

Install the hooks one time for each checkout:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## The Grid'5000 driver

It is easy to undo two properties of the driver by accident. The driver is
`workflow/grid_driver.py`. You run it as `language grid run`.

**Never send the CLI calls of the driver through `uv run`.** `uv run` holds a
lock on the shared uv cache for the whole command. The driver calls the CLI one
time for each shard. A process that holds the lock starts a child that waits
for the lock. Seven drivers in parallel were blocked for thirty minutes with no
child process. For this reason, `_cli()` returns the console script that is
next to `sys.executable`. A contract test pins this behavior.

**`shard_is_complete` is O(all shards). It is not O(1).** It runs a full
run-directory validate. For a directory with 384 shards, this took 3 m 40 s.
Only 0.63 s was CPU. The rest was I/O wait. Thus the per-shard loop of the
driver is quadratic. To discover the work across a partition of 55 shards, the
driver needs hours before it submits one job. This is a real defect. It is the
improvement with the highest value for this tooling. Until the team fixes it,
name the few shards that you must finish. Do not let the driver discover them.
Use a run directory that has only those shards.

## Test-driven changes

1. Add a focused failing pytest or contract test.
2. Verify the intended RED failure.
3. Implement the smallest change.
4. Verify GREEN before you run the full gate.

The integration tests use committed synthetic OSM fixtures and the installed
osmium binary. They do not use the Seagate roots or live Hub APIs.

## Documentation site

MkDocs Material builds the public documentation:

```bash
uv run mkdocs serve
uv run mkdocs build --strict
```

The `docs` GitHub Actions workflow builds the strict site again after each push
to `main`. It deploys the site to GitHub Pages. A repository administrator must
select **Settings → Pages → GitHub Actions** as the Pages source one time.

Internal planning material is not part of the public site. The team maintains
the generated dataset-card template separately. The team publishes it as
dataset metadata.

## CI

GitHub Actions does these steps:

1. It installs the locked uv environment.
2. It runs pre-commit, Ruff, and ty.
3. It runs pytest with at least 90% coverage.
4. It runs the strict MkDocs build and a wheel-content check.

CI has no Hugging Face credentials. It cannot publish the dataset.

## Benchmarks

`just bench` runs the synthetic, seeded micro-benchmarks in `benchmarks/`
(transform, H3 density-map rendering, CLI import time). To make them larger
locally, set `PERF_SCALE`. `just bench-save` and `just bench-compare` keep a
local baseline. They fail on a mean regression of more than 50%. The advisory
`benchmarks` job of CI measures the base and the head on the same runner.

Reference baseline for the renderer: 26.5k cells took 20.5 s with one patch for
each cell. They took about 6.7 s with a single `PolyCollection`.

## Shared sentence-model capabilities

See [Shared SaT capabilities](sat-capabilities.md) for ownership, offline pins,
cross-repository drift checks, and the reviewed update procedure.
