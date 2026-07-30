# Development

Python 3.12 and uv define the reproducible environment:

```bash
uv sync --locked
```

Runtime commands use a Typer CLI. Rich and tqdm provide interactive stderr
presentation while stdout stays machine-readable.

## Everyday commands

Just is the documented task runner:

```bash
just format
just lint
just typecheck
just test
just test-integration
just build
just check
```

The equivalent direct quality commands are:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
HF_HUB_OFFLINE=1 uv run pytest
```

Ruff is the sole formatter and linter. ty is the sole static type checker;
mypy is not part of the project. pytest owns unit, contract, and integration
tests.

## Test-driven changes

Add a focused failing pytest test, confirm the failure, implement the smallest
change, and confirm the focused test passes before running the complete gates.
Keep public compatibility shims when moving supported imports, and avoid
combining behavior changes with package moves.

## Local hooks and CI

Install pre-commit once per checkout:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

The hooks run repository hygiene, Ruff, ty, and contract tests. GitHub Actions
runs the locked uv environment, the same quality gates, the full offline
pytest suite, and a wheel-content check. CI must never authenticate to or
publish on Hugging Face.

## Operational boundary

Development and verification do not authorize a real source build or Hub
publication. The single stoppable and resumable production command is:

```bash
just run-and-publish
```

It uses the immutable raw-source and generated-data roots documented in the
root README.
