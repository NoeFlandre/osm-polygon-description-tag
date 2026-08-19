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
just docker-run "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
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
