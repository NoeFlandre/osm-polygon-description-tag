# Contributing

Thank you for your help. This repository builds a public dataset. Its quality
gates are strict. This page lists the gates. Use them locally to avoid failures
in CI. [docs/development.md](docs/development.md) has the full detail.

## Setup

```bash
uv sync --locked
uv run pre-commit install
```

The tests never read a real PBF root. They never contact Hugging Face.

## Before you open a pull request

| Gate | Run locally | Rule |
| --- | --- | --- |
| Format and lint | `just lint` | Ruff format and check have no errors |
| Types | `just typecheck` | `ty` with error-on-warning, over `src/` and `scripts/` |
| Tests and coverage | `just test` | The full suite passes. Total coverage is 90% or more |
| Complexity (CRAP) | `just risk` | Each function scores less than 6. `scripts/crap-allowlist.json` lists the only exceptions. The list expires |
| Mutation | `just mutation-scope <changed-lines file>` ([how](docs/development.md)) | The tests kill each mutant on the lines that you change |
| Docs | `uv run mkdocs build --strict` | The build gives no warnings |
| All gates except mutation | `just check` | It also runs pre-commit and the build |

Write the failing test first. Then make the change. Use one issue for each
commit. Add the reference (`Closes #NN`) to the commit or to the pull request.

## Commits

Start the message with a short conventional prefix (`fix:`, `perf:`, `test:`,
`docs:`, `ci:`, `build:`, `refactor:`). Explain *why* in the body. Add an entry
under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for each change that the
user can see.

## Reporting a vulnerability

Do not open a public issue. Refer to [SECURITY.md](SECURITY.md).
