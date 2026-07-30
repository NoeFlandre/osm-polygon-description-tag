# Development

Use uv as the project and dependency runner:

```bash
uv sync
uv run pytest -q
```

Run focused tests while developing, then the full suite:

```bash
uv run pytest tests/contracts/test_package_layout.py -q
uv run pytest -q
```

Use Ruff for formatting and linting:

```bash
uv run ruff format --check .
uv run ruff check .
```

The current type-checking command is:

```bash
uv run ty check
```

## Change workflow

Use test-driven development: add a focused failing test, confirm the expected failure, make the
smallest implementation change, and confirm the focused test passes. Keep compatibility contracts
when moving a public import, and avoid combining behavior changes with package moves.

## Release gates

Before completion, run formatting and lint checks, type checking, the full pytest suite, and any
integration test whose external dependency is available. A local test run does not authorize a
real source-data build or Hub publication.
