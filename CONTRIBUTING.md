# Contributing

Thanks for helping. This repository builds a public dataset, so its quality
gates are strict. This page lists them so you meet them locally instead of in
CI. [docs/development.md](docs/development.md) has the full detail.

## Setup

```bash
uv sync --locked
uv run pre-commit install
```

Tests never read a real PBF root or contact Hugging Face.

## Before you open a pull request

| Gate | Run locally | Rule |
| --- | --- | --- |
| Format and lint | `just lint` | Ruff format and check are clean |
| Types | `just typecheck` | `ty` with error-on-warning, over `src/` and `scripts/` |
| Tests and coverage | `just test` | Full suite passes; total coverage ≥ 90% |
| Complexity (CRAP) | `just risk` | Every function scores < 6 (`scripts/crap-allowlist.json` lists the only exceptions, and it expires) |
| Mutation | `just mutation-scope <changed-lines file>` ([how](docs/development.md)) | Every mutant on the lines you changed is killed |
| Docs | `uv run mkdocs build --strict` | Builds without warnings |
| Everything above except mutation | `just check` | Also runs pre-commit and the build |

Write the failing test first, then the change. Keep one issue per commit and
reference it (`Closes #NN`) in the commit or pull request.

## Commits

Use a short conventional prefix (`fix:`, `perf:`, `test:`, `docs:`, `ci:`,
`build:`, `refactor:`) and explain *why* in the body. Add an entry under
`[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything user-visible.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
