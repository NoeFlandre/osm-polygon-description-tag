# CLI and Tooling Modernization Design

## Purpose

Standardize the public project on uv, Ruff, ty, pytest, pre-commit, Typer,
Rich, tqdm, Just, and GitHub Actions without changing dataset artifacts,
resumability, publication safety, command names, option names, successful JSON
payloads, or exit-code semantics.

This amendment extends the approved domain-package organization work. It does
not authorize a real PBF run, source-data mutation, generated-data mutation,
Hugging Face authentication or publication, remote dataset cleanup, or a Git
push.

## Current state

The project currently:

- uses uv and a committed lockfile;
- uses Ruff for formatting and linting;
- uses pytest and pytest-cov;
- still uses mypy rather than ty;
- implements the public CLI with `argparse`;
- has no Typer, Rich, tqdm, pre-commit, Just, or GitHub Actions configuration;
- emits deterministic JSON on successful stdout and operational/error text on
  stderr;
- exposes one stoppable, resumable `run-and-publish` command.

The modernization must preserve the observable CLI and pipeline contracts.

## Considered approaches

### Cohesive tooling and CLI migration

Replace argparse fully with Typer, add Rich and tqdm at the presentation
boundary, and establish one consistent local/CI toolchain. This removes the
old framework rather than layering abstractions and gives operators a polished
interactive experience without altering machine-facing output.

This is the selected approach.

### Typer compatibility wrapper

Keep argparse internally and expose it through Typer. This reduces immediate
movement but leaves two parsers, duplicated error semantics, and an unclear
canonical CLI API.

### Optional presentation dependencies

Make Rich and tqdm optional with plain-text fallbacks. This reduces the
required install set slightly but creates extra runtime branches and test
matrices in a single-purpose public CLI.

## Public CLI contract

Typer fully replaces argparse. No argparse compatibility wrapper or parser
object remains in production code.

The following remain stable:

- console command `osm-polygon-description-tag`;
- subcommands `inspect`, `build-one`, `build-all`, `validate`,
  `generate-card`, `publish-plan`, `publish`, and `run-and-publish`;
- existing option and positional names;
- source/data defaults and osmium executable selection;
- required exact plan and repository confirmations;
- successful JSON object structures;
- ordinary failure exit code 1;
- usage/validation failure exit code 2;
- keyboard-interrupt exit code 130.

Command functions remain thin presentation adapters over canonical runtime,
OSM, dataset, publication, and workflow APIs.

## Output streams and presentation

Successful stdout remains deterministic, UTF-8 JSON only. It never contains
Rich markup, progress bars, banners, spinners, warnings, or diagnostics.

Human-facing errors and operational presentation use stderr:

- when stderr is an interactive TTY, Rich may add styling;
- when stderr is redirected, captured, or running in CI, output is plain text;
- text content remains stable enough for the existing error contracts;
- credentials, environment dumps, and command lines remain excluded.

tqdm writes only to interactive stderr. It is disabled automatically for
non-TTY streams, tests, CI, and redirection. Progress is bounded and
source-scoped; it does not emit per dataset row. tqdm is a view over existing
typed lifecycle/progress events and never replaces:

- the persistent rotated JSONL operational log;
- atomic publication state;
- manifests;
- the final JSON report.

A no-op resumable run may append operational log events but does not mutate
dataset artifacts or publication state, exactly as before.

## Dependency and configuration contract

uv remains the sole Python dependency manager and command runner. `uv.lock`
records the resolved versions.

Runtime dependencies add:

- Typer for command declaration and parsing;
- Rich for interactive stderr presentation;
- tqdm for interactive progress.

Development dependencies contain:

- Ruff;
- ty;
- pytest;
- pytest-cov;
- pre-commit.

Mypy is removed completely from active dependencies, lockfile, configuration,
commands, automation, and operator/developer documentation. Historical design
records remain unchanged.

Argparse is removed from production source and active CLI documentation.

## Pre-commit contract

The root `.pre-commit-config.yaml` runs hermetic repository checks:

- standard whitespace, EOF, YAML, TOML, and large-file safety hooks;
- Ruff formatting;
- Ruff linting;
- ty;
- a focused fast pytest contract suite.

Hooks do not access Hugging Face, the network, real PBFs, the immutable raw
root, or the generated-data root. Full integration and coverage gates remain
CI/release responsibilities rather than slowing every commit.

## Just command contract

A root `justfile` exposes memorable commands while delegating Python execution
to uv:

```text
just sync
just format
just lint
just typecheck
just test
just test-integration
just build
just check
just run-and-publish
```

`just check` runs the authoritative local verification sequence without
external publication.

`just run-and-publish` expands to the one production operation:

```bash
uv run osm-polygon-description-tag run-and-publish \
  --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
  --confirm-repo NoeFlandre/osm-polygon-description-tag
```

The recipe is stoppable with Ctrl-C, returns 130, and is safely resumable by
running the same recipe again. It does not hide or broaden the command scope.

## GitHub Actions contract

GitHub Actions uses Ubuntu and Python 3.12. The workflow installs:

- uv using the maintained official setup action;
- `osmium-tool` through the Ubuntu package manager;
- the project strictly from the committed lockfile.

The workflow runs:

```bash
uv lock --check
uv run pre-commit run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov=osm_polygon_description_tag \
  --cov-report=term-missing --cov-fail-under=90
uv build
```

It then checks wheel contents and smoke-tests CLI help. Tests must have zero
failures and zero skips.

Real-osmium integration tests run only against committed synthetic fixtures.
CI has no Hugging Face credentials and must not contact the dataset Hub.
Hermetic guards reject accidental Hub API calls and auth/upload subprocesses.
CI never reads or writes either Seagate root.

## Testing strategy

All work follows RED, GREEN, and refactor.

Before the Typer migration, contract tests pin:

- command and option names;
- required arguments;
- JSON-only successful stdout;
- plain non-interactive stderr;
- failure/usage/interrupt exit codes;
- existing command payloads;
- absence of test-only public CLI hooks.

Presentation tests inject explicit terminal capability rather than depending
on the test runner's terminal:

- non-TTY stdout stays JSON-only;
- non-TTY stderr stays unstyled;
- TTY stderr enables Rich;
- tqdm appears only on TTY stderr;
- progress does not enter stdout or JSONL schemas unexpectedly.

Lifecycle integration tests continue to prove interruption, restart,
validated-local reuse, per-PBF upload, final metadata publication, and a
third-run no-op.

Toolchain contracts verify:

- uv and the committed lockfile;
- Ruff format/lint configuration;
- ty configuration and complete mypy removal;
- pytest/coverage configuration;
- pre-commit hooks;
- runtime Typer/Rich/tqdm dependencies;
- the Just recipes and exact production command;
- the GitHub Actions environment and gates;
- complete argparse removal from production source.

## Documentation

The root README remains operator-focused and documents both the exact uv
command and `just run-and-publish`.

`docs/development.md` documents setup, individual uv gates, equivalent Just
recipes, pre-commit installation/use, synthetic integration requirements, and
the GitHub Actions parity contract.

The package and architecture documentation describe Typer/Rich/tqdm as a
presentation boundary. They do not duplicate generated dataset statistics or
claim that interactive progress is durable state.

## Implementation integration

This amendment is integrated into the remaining domain-package implementation
instead of starting an unrelated parallel refactor:

1. finish the publication and workflow package splits without CLI changes;
2. enforce canonical dependency direction;
3. finalize test organization;
4. replace mypy with ty and add pre-commit/toolchain contracts;
5. migrate argparse to Typer under pinned CLI contracts;
6. add Rich/tqdm presentation adapters;
7. add the Just recipes and GitHub Actions workflow;
8. finalize documentation, wheel checks, and the complete verification gate.

Each step is independently committed and keeps the CLI executable.

## Acceptance criteria

The amendment is complete only when:

- all existing public commands and flags remain available;
- successful stdout is deterministic JSON only;
- interactive Rich/tqdm output is confined to TTY stderr;
- redirected/CI stderr is plain and progress-free;
- Ctrl-C returns 130 and the same command resumes safely;
- argparse and mypy are absent from active production/tooling configuration;
- uv, Ruff, ty, pytest, pre-commit, Typer, Rich, tqdm, Just, and GitHub Actions
  are configured, tested, and documented;
- pre-commit and CI are hermetic with respect to Hub and external data;
- synthetic real-osmium integration tests pass in CI;
- full coverage remains at least 90 percent with zero failures and zero skips;
- wheel contents and CLI smoke tests pass;
- no real PBF is opened and no external root is mutated during implementation;
- no Hugging Face or GitHub mutation occurs without later explicit
  authorization.
