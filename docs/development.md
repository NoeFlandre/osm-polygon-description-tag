# Development

## Environment

Python 3.12 and uv define the locked environment:

```bash
uv sync --locked
```

The project uses Ruff for formatting/linting, ty for static typing, pytest for
tests, pre-commit for local hooks, Just for named workflows, Typer for the CLI,
Rich/tqdm for interactive stderr presentation, and GitHub Actions for CI.

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
pytest coverage gate, and the package build. It never reads the real PBF root
or contacts Hugging Face.

`just risk` writes ignored reports under `reports/`. It combines pytest coverage
with Radon cyclomatic complexity and calculates the deterministic CRAP score
(`complexity² × (1 − coverage)³ + complexity`) for every source function. The
repository-wide budget is strict: every function must remain below 6.
`just mutation` checks generated mutants across all source modules.
It uses a short deterministic triage pass, then reruns every survivor, timeout,
and untested mutant against its complete mutmut association. The gate requires
100% of generated mutants killed with no unresolved statuses, subject to
documented source-level exclusions. The full regression suite, including
contract tests, remains a separate required gate.
Mutmut's `mutants/` cache is ignored and may be reused locally; CI runs both
repository-wide gates as required jobs.

### Running the mutation gate somewhere fast

The gate is I/O bound, not CPU bound. On the external `Seagate M3` volume that
holds this checkout, reading 400 small source files cold takes about 12 seconds
--- roughly 30 ms each --- and the gate re-reads the source tree for every
mutant. Measured on the same machine, one full suite run took over 40 minutes
from the volume and 152 seconds from the internal disk.

So run the gate from a scratch copy on a fast local disk and keep the volume as
the source of truth:

1. copy `src/`, `tests/`, `scripts/`, `pyproject.toml` and `uv.lock` to a
   scratch directory, and `uv sync --frozen --all-extras` there;
2. copy `mutants/**/*.py.meta`, `mutants/mutmut-stats.json` and
   `mutants/mutmut-recorded-tests.json` across so the run resumes instead of
   starting from zero --- mutmut regenerates the mutant sources itself;
3. run `just mutation-contexts` and the gate there;
4. copy the `.py.meta` files back, and delete the scratch copy.

Keep `TMPDIR` outside the copied tree: several tests walk up from a temporary
path looking for `pyproject.toml`, and a `TMPDIR` inside the project makes them
find the wrong one. Give the gate a `TMPDIR` nothing else uses, too: sharing one
with an ad-hoc `pytest` run deletes the numbered directory underneath it, and
the gate's stats collection then dies with `FileNotFoundError`.

Always export `MUTATION_TMP_ROOT` as well. The gate starts a janitor that reaps
each mutant's abandoned scratch directory, but only when that variable is set,
so the safeguard is off by default and nothing warns you. One interrupted run
without it left **79 586** pytest directories totalling 5.0 GB.

`--mutation-batch-size` is worth raising from its default of 4. Every batch is
one `mutmut` invocation, and each invocation re-scans the whole source tree
before running anything (~2.3 s). At the default an 18 000-mutant run pays that
roughly 2 987 times --- about two hours of pure overhead; at 400 it pays it
about 52 times.

Do not run the gate beside other heavy CPU work. A contended run produced three
verdicts that were simply wrong: one mutant reported `survived` that its own
existing test kills outright, and two reported `segfault` that fail 19 and 21
tests respectively. Confirm any survivor individually before writing a test for
it:

```bash
PYTHONPATH=mutants/src MUTANT_UNDER_TEST=<mutant> \
  .venv/bin/python -m pytest <its test file> -q --no-cov
```

### Refresh the gate's test selection after adding tests

Per-test coverage contexts override mutmut's recorded association, so a stale
context file hides newly added tests from selection: they never run, and the
mutants they kill keep reporting as survivors. After adding or renaming tests,
re-record `just mutation-contexts` and delete `mutants/mutmut-stats.json` and
`mutants/mutmut-recorded-tests.json` before running the gate. Renaming a
parametrised case matters most --- the stale recording still names the old test
ids, and the run aborts in its clean preflight with `not found:`.

### A pragma only works on a single-line statement

Mutmut records a trailing `# pragma: no mutate` against the line the *statement*
starts on, so a pragma written against one argument of a multi-line call is
silently ignored. `ruff format` compounds this by moving a trailing comment onto
a closing bracket. Use the `# pragma: no mutate start` / `end` block form for a
multi-line statement, remembering that it suppresses every mutation in the
block, and confirm any new pragma by checking that the mutant it targets stops
being generated.

### Timeouts are unresolved verdicts, not kills

A timeout is not a killed mutant for this gate, and mutmut's limit is wall
clock: `(estimated_test_time + timeout_constant) * timeout_multiplier`, where
the estimate comes from an unloaded baseline run. On a shared machine that
estimate is optimistic, so contention alone turns killable mutants into
timeouts --- one contended sweep here reported 389 of them. `timeout_constant`
and `timeout_multiplier` in `pyproject.toml` are therefore set well above the
slowest honest run. Changing either value makes mutmut discard its cached
timeout verdicts and re-run them, which is what you want after the machine
quietens down.

## Docker reproducibility

The checked-in `Dockerfile` copies the version-pinned uv binary from
`ghcr.io/astral-sh/uv:0.11.16` into Python 3.12 Bookworm slim, installs the
runtime graph from `uv.lock`, and includes Debian's `osmium-tool` binary. The
runtime image contains only the non-editable installed package, runs as an
unprivileged `app` user, and defaults to `--help` so it cannot start the
pipeline accidentally. For immutable uv provenance, pass a verified digest
with `--build-arg UV_IMAGE=...@sha256:...`.

Build and run the safe container checks:

```bash
just docker-build
just docker-help
just docker-test
just docker-check
```

The single production command builds or reuses the runtime image, mounts the
external generated-data root at `/data`, and mounts its `/data/raw` source
directory read-only:

```bash
just docker-run "/Volumes/Seagate M3/projects/osm-polygon-description-tag/data-root"
```

The mounted data root retains Parquets, manifests, logs, caches, and
publication state. `Ctrl-C` is safe; rerunning the command resumes completed
sources. `HF_TOKEN` is passed from the host only for the explicit Hub
publication step. Docker build, help, and test commands do not read real PBFs
or contact Hugging Face.

Install hooks once per checkout:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## The Grid'5000 driver

Two properties of `scripts/run_language_grid.py` are easy to undo by accident.

**Never route the driver's CLI calls through `uv run`.** `uv run` holds a lock
on the shared uv cache for the whole command, and the driver invokes the CLI
once per shard, so a process holding that lock spawns a child that waits for it.
Seven drivers in parallel wedged for thirty minutes with no child process at
all. `_cli()` returns the console script beside `sys.executable` for this
reason, and a contract test pins it.

**`shard_is_complete` is O(all shards), not O(1).** It runs a full run-directory
validate, which against a directory holding 384 shards measured 3 m 40 s --- 0.63 s
of CPU, the rest blocked on I/O. The driver's per-shard loop is therefore
quadratic: discovering work across a 55-shard partition costs hours before a
single job is submitted. This is a real defect and the highest-value improvement
available to this tooling. Until it is fixed, finish a known handful of shards
by naming them rather than letting the driver discover them, and use a run
directory that holds only those shards.

## Test-driven changes

Add a focused failing pytest or contract test, verify the intended RED
failure, implement the smallest change, and verify GREEN before running the
full gate. Integration tests use committed synthetic OSM fixtures and the
installed osmium binary. They do not use the Seagate roots or live Hub APIs.

## Documentation site

The public documentation is built with MkDocs Material:

```bash
uv run mkdocs serve
uv run mkdocs build --strict
```

The `docs` GitHub Actions workflow rebuilds and deploys the strict site to
GitHub Pages after every push to `main`. A repository administrator must select
**Settings → Pages → GitHub Actions** as the Pages source once.

Internal planning material is not part of the public site. The generated
dataset-card template is maintained separately because it is published as
dataset metadata.

## CI

GitHub Actions installs the locked uv environment, runs pre-commit, Ruff, ty,
pytest with at least 90% coverage, the strict MkDocs build, and a wheel-content
check. CI has no Hugging Face credentials and cannot publish the dataset.
