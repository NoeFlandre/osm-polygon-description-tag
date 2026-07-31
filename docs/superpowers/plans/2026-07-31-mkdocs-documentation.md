# MkDocs Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a strict MkDocs Material site and polish the repository documentation without changing the data pipeline contract.

**Architecture:** Add a small public documentation site under `docs/` with a
versioned `mkdocs.yml`, while retaining package READMEs and the generated
dataset-card template as separate artifact documentation. Add a hermetic docs
contract test and a CI build gate; no site task reads Seagate data or contacts
Hugging Face.

**Tech Stack:** MkDocs Material, Python 3.12, uv, pytest, Ruff, ty, GitHub Actions.

---

### Task 1: Pin the documentation-site contract

**Files:**
- Create: `tests/contracts/test_mkdocs_site.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] Write tests requiring `mkdocs.yml`, the seven public Markdown pages,
  `mkdocs-material` in the development dependency group, and a nav entry for
  each page.
- [ ] Run the focused test and verify it fails because MkDocs is not configured.
- [ ] Add `mkdocs-material>=9.6,<10` to the uv development group and run
  `uv lock`.
- [ ] Verify the focused contract passes and commit the dependency/site
  contract.

### Task 2: Add the public MkDocs site

**Files:**
- Create: `mkdocs.yml`
- Create: `docs/index.md`
- Create: `docs/getting-started.md`
- Create: `docs/dataset-contract.md`
- Create: `docs/operations.md`
- Create: `docs/cli.md`
- Modify: `docs/development.md`
- Modify: `docs/architecture.md`
- Modify: `README.md`

- [ ] Write the pages from the approved accuracy rules: no fabricated counts,
  exact paths, exact commands, complete geometry/area/tags contract, and
  explicit publication boundaries.
- [ ] Configure Material navigation, search, code-copy, strict mode, and
  exclusion of historical `docs/superpowers/` files.
- [ ] Make the root README a concise landing page linking to the site pages.
- [ ] Run `uv run mkdocs build --strict` and fix all warnings.
- [ ] Commit the site and documentation rewrite.

### Task 3: Add CI verification and synchronize GitHub

**Files:**
- Modify: `.github/workflows/quality.yml`
- Modify: `tests/contracts/test_mkdocs_site.py` if needed for final behavior.

- [ ] Add `uv run mkdocs build --strict --site-dir /tmp/...` to CI after the
  locked environment is installed.
- [ ] Run pre-commit, Ruff format/lint, ty, the docs contract, strict MkDocs,
  the full pytest coverage gate, and the wheel build locally.
- [ ] Confirm the worktree is clean and no Seagate root was accessed.
- [ ] Commit any final CI/test changes.
- [ ] Push the complete local `main` history to `origin/main` and verify the
  remote SHA equals local `HEAD`.
