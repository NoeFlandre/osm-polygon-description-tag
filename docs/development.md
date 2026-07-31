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
uv run mkdocs build --strict
just check
```

`just check` runs the locked dependency check, pre-commit, Ruff, ty, the full
pytest coverage gate, and the package build. It never reads the real PBF root
or contacts Hugging Face.

Install hooks once per checkout:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

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

Historical design and implementation plans under `docs/superpowers/` are
excluded from the public site navigation. The generated dataset-card template
is maintained separately because it is published as dataset metadata.

## CI

GitHub Actions installs the locked uv environment, runs pre-commit, Ruff, ty,
pytest with at least 90% coverage, the strict MkDocs build, and a wheel-content
check. CI has no Hugging Face credentials and cannot publish the dataset.
