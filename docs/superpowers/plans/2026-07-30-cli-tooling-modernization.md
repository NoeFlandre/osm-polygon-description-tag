# CLI and Tooling Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace argparse and mypy with a Typer/Rich/tqdm CLI and ty, then add hermetic pre-commit, Just, and GitHub Actions workflows while preserving the public pipeline contract.

**Architecture:** Typer owns parsing but delegates to the existing canonical domain APIs. A dependency-injected terminal presenter observes typed run events and renders Rich/tqdm output only to interactive stderr; stdout and persistent JSONL remain unchanged. Local uv commands are wrapped by Just and reproduced by pre-commit and GitHub Actions.

**Tech Stack:** Python 3.12, uv, Typer, Rich, tqdm, Ruff, ty, pytest, pytest-cov, pre-commit, Just, GitHub Actions, osmium-tool.

---

## Execution order

Execute this plan only after Tasks 5–8 of
`docs/superpowers/plans/2026-07-30-domain-package-organization.md`. Those tasks
finish the publication/workflow splits, canonical dependency direction, and
test layout before the CLI imports are migrated.

## File map

- Modify `pyproject.toml` and `uv.lock` for the authoritative dependency set.
- Create `tests/contracts/test_toolchain_contract.py`.
- Replace `src/osm_polygon_description_tag/cli.py` internals with Typer while
  retaining `run(argv) -> int`.
- Create `src/osm_polygon_description_tag/runtime/presentation.py`.
- Modify `runtime/logging.py` only to support an optional typed event observer.
- Create `tests/unit/runtime/test_presentation.py`.
- Modify CLI and lifecycle contracts/integration tests.
- Create `.pre-commit-config.yaml`, `justfile`, and
  `.github/workflows/quality.yml`.
- Update root/package/development/architecture documentation.

### Task 1: Replace mypy with ty and add the required dependency set

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/contracts/test_toolchain_contract.py`

- [ ] **Step 1: Write the failing dependency contract**

```python
from pathlib import Path

import tomllib
from typing import Any, cast


PROJECT_ROOT = Path(__file__).parents[2]


def _pyproject() -> dict[str, object]:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_required_runtime_and_development_tools_are_declared() -> None:
    config = cast(dict[str, Any], _pyproject())
    runtime = tuple(config["project"]["dependencies"])
    dev = tuple(config["dependency-groups"]["dev"])
    assert any(item.startswith("typer") for item in runtime)
    assert any(item.startswith("rich") for item in runtime)
    assert any(item.startswith("tqdm") for item in runtime)
    for name in ("ruff", "ty", "pytest", "pytest-cov", "pre-commit"):
        assert any(item.startswith(name) for item in dev)
    assert all(not item.startswith("mypy") for item in dev)


def test_ty_replaces_mypy_configuration() -> None:
    config = cast(dict[str, Any], _pyproject())
    tools = config["tool"]
    assert "mypy" not in tools
    assert tools["ty"]["environment"]["python-version"] == "3.12"
    assert tools["ty"]["environment"]["root"] == ["./src"]
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/contracts/test_toolchain_contract.py -q`

Expected: FAIL because Typer/Rich/tqdm/ty/pre-commit and `[tool.ty]` are absent
and mypy is present.

- [ ] **Step 3: Update dependencies through uv**

Run:

```bash
uv remove --dev mypy
uv add "typer>=0.12,<1" "rich>=13,<15" "tqdm>=4.66,<5"
uv add --dev ty pre-commit
```

Expected: `pyproject.toml` and `uv.lock` contain the new runtime/development
dependencies and no mypy package.

- [ ] **Step 4: Replace the type-checker configuration**

Delete `[tool.mypy]` and all mypy overrides. Add:

```toml
[tool.ty.environment]
python-version = "3.12"
root = ["./src"]

[tool.ty.terminal]
error-on-warning = true
```

- [ ] **Step 5: Run GREEN and resolve canonical diagnostics**

Run:

```bash
uv lock --check
uv run ty check
uv run pytest tests/contracts/test_toolchain_contract.py -q
```

Expected: all exit 0. Fix concrete ty diagnostics at canonical definitions;
do not add blanket ignore rules or replace third-party modules with `Any`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tests/contracts/test_toolchain_contract.py
git commit -m "chore: standardize dependencies on ty tooling"
```

### Task 2: Pin the existing CLI before changing parser frameworks

**Files:**
- Modify: `tests/contracts/test_cli_contract.py`
- Modify: `tests/unit/workflow/test_cli_no_test_hooks.py`
- Create: `tests/contracts/test_cli_streams.py`

- [ ] **Step 1: Add command and option contract assertions**

Use the public subprocess boundary so the tests are parser-agnostic:

```python
import subprocess
import sys


COMMANDS = (
    "inspect",
    "build-one",
    "build-all",
    "validate",
    "generate-card",
    "publish-plan",
    "publish",
    "run-and-publish",
)


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "osm_polygon_description_tag.cli", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_all_public_commands_remain_available() -> None:
    result = _cli("--help")
    assert result.returncode == 0
    assert all(command in result.stdout for command in COMMANDS)


def test_usage_error_remains_exit_two() -> None:
    result = _cli("publish")
    assert result.returncode == 2
```

Add focused help checks for `--source-root`, `--data-root`, `--osmium`,
`--plan`, `--confirm-repo`, and the `build-one` basename.

- [ ] **Step 2: Pin JSON-only stdout and plain redirected errors**

```python
import json


def test_success_stdout_is_one_json_document(monkeypatch, capsys) -> None:
    # Reuse the existing inspect dependency fakes from the CLI contract.
    exit_code = cli.run(["inspect", "--source-root", str(source_root), "--data-root", str(data_root)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["source_count"] == expected_count
    assert "\x1b[" not in captured.out


def test_domain_error_is_plain_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "discover_sources", lambda _root: (_ for _ in ()).throw(ValueError("boom")))
    exit_code = cli.run(["inspect"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "error: boom\n"
    assert "\x1b[" not in captured.err
```

Use existing fixtures and paths from the repository rather than introducing
real defaults into tests.

- [ ] **Step 3: Run the pinned contracts**

Run:

```bash
uv run pytest tests/contracts/test_cli_contract.py \
  tests/contracts/test_cli_streams.py \
  tests/unit/workflow/test_cli_no_test_hooks.py -q
```

Expected: PASS against the current argparse CLI. These are characterization
tests, so no production change occurs in this task.

- [ ] **Step 4: Commit**

```bash
git add tests/contracts/test_cli_contract.py \
  tests/contracts/test_cli_streams.py \
  tests/unit/workflow/test_cli_no_test_hooks.py
git commit -m "test: pin public CLI and stream contracts"
```

### Task 3: Replace argparse with Typer

**Files:**
- Modify: `src/osm_polygon_description_tag/cli.py`
- Modify: `pyproject.toml`
- Modify: CLI contract/unit tests

- [ ] **Step 1: Add a failing Typer ownership contract**

Append to `test_toolchain_contract.py`:

```python
def test_typer_fully_owns_the_cli() -> None:
    cli_source = (
        PROJECT_ROOT / "src" / "osm_polygon_description_tag" / "cli.py"
    ).read_text(encoding="utf-8")
    assert "import argparse" not in cli_source
    assert "typer.Typer" in cli_source
    assert _pyproject()["project"]["scripts"]["osm-polygon-description-tag"] == (
        "osm_polygon_description_tag.cli:main"
    )
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/contracts/test_toolchain_contract.py::test_typer_fully_owns_the_cli -q
```

Expected: FAIL because argparse and the old `cli:run` entry point remain.

- [ ] **Step 3: Declare the Typer application and shared helpers**

Use this public shape:

```python
from typing import Annotated

import click
import typer

app = typer.Typer(
    name="osm-polygon-description-tag",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)

SourceRoot = Annotated[Path | None, typer.Option("--source-root")]
DataRoot = Annotated[Path | None, typer.Option("--data-root")]
Osmium = Annotated[str, typer.Option("--osmium")]


def _resolve_paths(source_root: Path | None, data_root: Path | None) -> Paths:
    defaults = Paths.defaults()
    return Paths(
        source_root=source_root or defaults.source_root,
        data_root=data_root or defaults.data_root,
    ).validate()
```

Retain `_print_json` unchanged.

- [ ] **Step 4: Convert every command directly**

Decorate one function per existing command:

```python
@app.command("inspect")
def inspect_command(
    source_root: SourceRoot = None,
    data_root: DataRoot = None,
    osmium: Osmium = "osmium",
) -> None:
    paths = _resolve_paths(source_root, data_root)
    sources = discover_sources(paths.source_root)
    _print_json({...})
```

Repeat with the exact existing payload construction for all eight commands.
Use `typer.Argument(...)` for `build-one` basename and `typer.Option(...,
"--plan")` / `typer.Option(..., "--confirm-repo")` for required
confirmations. Do not rename or reshape payloads.

- [ ] **Step 5: Preserve programmatic and console exit behavior**

```python
def run(argv: Sequence[str] | None = None) -> int:
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="osm-polygon-description-tag",
            standalone_mode=False,
        )
        return 0
    except click.exceptions.Exit as error:
        return int(error.exit_code)
    except click.ClickException as error:
        error.show(file=sys.stderr)
        return int(error.exit_code)
    except KeyboardInterrupt:
        return 130
    except _ERROR_TYPES as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
```

Export `app`, `main`, and `run` explicitly. Remove argparse parser/Namespace
helpers completely.

- [ ] **Step 6: Change the console entry point and run GREEN**

Set:

```toml
[project.scripts]
osm-polygon-description-tag = "osm_polygon_description_tag.cli:main"
```

Then run:

```bash
uv run pytest tests/contracts/test_cli_contract.py \
  tests/contracts/test_cli_streams.py \
  tests/contracts/test_toolchain_contract.py \
  tests/unit/workflow/test_cli_no_test_hooks.py -q
uv run osm-polygon-description-tag --help
uv run osm-polygon-description-tag run-and-publish --help
uv run ty check
```

Expected: PASS, with all commands/options present and exit semantics pinned.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag/cli.py pyproject.toml uv.lock \
  tests/contracts tests/unit/workflow/test_cli_no_test_hooks.py
git commit -m "refactor: replace argparse CLI with Typer"
```

### Task 4: Add Rich and tqdm as an interactive stderr presentation boundary

**Files:**
- Create: `src/osm_polygon_description_tag/runtime/presentation.py`
- Modify: `src/osm_polygon_description_tag/runtime/logging.py`
- Modify: `src/osm_polygon_description_tag/runtime/__init__.py`
- Create: `tests/unit/runtime/test_presentation.py`
- Modify: `tests/unit/runtime/test_logging_architecture.py`
- Modify: `src/osm_polygon_description_tag/cli.py`

- [ ] **Step 1: Write failing presenter tests**

```python
from io import StringIO

from osm_polygon_description_tag.runtime.presentation import TerminalPresenter


class TerminalBuffer(StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_non_tty_is_plain_and_has_no_progress() -> None:
    stream = TerminalBuffer(tty=False)
    presenter = TerminalPresenter(stderr=stream)
    presenter.observe({"event": "build_progress", "source": "a.osm.pbf", "emitted": 100_000})
    assert stream.getvalue() == ""
    assert presenter.progress_active is False


def test_tty_build_progress_uses_tqdm() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)
    presenter.observe({"event": "source_start", "source": "a.osm.pbf", "source_total": 2})
    presenter.observe({"event": "build_progress", "source": "a.osm.pbf", "emitted": 100_000})
    presenter.close()
    assert "a.osm.pbf" in stream.getvalue()
    assert presenter.progress_active is False


def test_tty_error_uses_rich_but_never_stdout() -> None:
    stream = TerminalBuffer(tty=True)
    presenter = TerminalPresenter(stderr=stream)
    presenter.error("boom")
    assert "boom" in stream.getvalue()
```

Also assert ANSI escapes never appear for `tty=False`.

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/unit/runtime/test_presentation.py -q`

Expected: FAIL because `runtime.presentation` does not exist.

- [ ] **Step 3: Implement the focused presenter**

```python
class TerminalPresenter:
    def __init__(self, *, stderr: TextIO = sys.stderr) -> None:
        self._stderr = stderr
        self._interactive = bool(getattr(stderr, "isatty", lambda: False)())
        self._console = Console(file=stderr, force_terminal=self._interactive, color_system="auto")
        self._progress: tqdm[object] | None = None

    @property
    def progress_active(self) -> bool:
        return self._progress is not None

    def observe(self, event: Mapping[str, object]) -> None:
        if not self._interactive:
            return
        name = event.get("event")
        if name == "source_start":
            self.close()
            self._progress = tqdm(
                total=None,
                desc=str(event.get("source", "")),
                unit="features",
                file=self._stderr,
                disable=False,
            )
        elif name == "build_progress" and self._progress is not None:
            emitted = int(event.get("emitted", 0))
            self._progress.update(max(0, emitted - self._progress.n))
        elif name in {"build_complete", "source_complete", "interrupted"}:
            self.close()

    def error(self, message: str) -> None:
        if self._interactive:
            self._console.print(f"[bold red]error:[/bold red] {message}")
        else:
            self._stderr.write(f"error: {message}\n")
            self._stderr.flush()

    def close(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
```

Use concrete `tqdm` typing supported by the installed release; do not silence
ty broadly.

- [ ] **Step 4: Add a no-op observer seam to RunLogger**

Extend `RunLogger.__init__` with:

```python
observer: Callable[[Mapping[str, object]], None] | None = None,
```

Store it and, after redaction but before human/persistent emission, call it
best-effort with a copy:

```python
if self._observer is not None:
    try:
        self._observer(dict(scrubbed))
    except Exception:
        pass
```

Add a test proving observer failure cannot suppress stderr or JSONL logging,
and that the observer receives redacted rather than credential-bearing data.

- [ ] **Step 5: Inject presentation from the Typer boundary**

Create one `TerminalPresenter(stderr=sys.stderr)` for `run-and-publish`, pass
`presenter.observe` through the existing workflow logger factory/injection
seam, and close it in `finally`. Route domain error rendering through
`presenter.error`; keep usage errors owned by Click/Typer. No presenter may
write stdout.

- [ ] **Step 6: Run GREEN and lifecycle tests**

Run:

```bash
uv run pytest tests/unit/runtime/test_presentation.py \
  tests/unit/runtime/test_logging_architecture.py \
  tests/contracts/test_cli_streams.py \
  tests/integration/test_public_cli_lifecycle.py \
  tests/integration/test_three_run_public.py -q
uv run ty check
uv run ruff format --check .
uv run ruff check .
```

Expected: PASS; redirected tests contain no progress/ANSI and lifecycle
resumability remains unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/osm_polygon_description_tag/runtime \
  src/osm_polygon_description_tag/cli.py \
  tests/unit/runtime tests/contracts/test_cli_streams.py tests/integration
git commit -m "feat: add interactive terminal presentation"
```

### Task 5: Add hermetic pre-commit and Just recipes

**Files:**
- Create: `.pre-commit-config.yaml`
- Create: `justfile`
- Modify: `tests/contracts/test_toolchain_contract.py`

- [ ] **Step 1: Add failing configuration contracts**

```python
def test_pre_commit_and_just_are_configured() -> None:
    pre_commit = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    for token in ("ruff format", "ruff check", "ty check", "pytest"):
        assert token in pre_commit
    for recipe in (
        "sync:",
        "format:",
        "lint:",
        "typecheck:",
        "test:",
        "test-integration:",
        "build:",
        "check:",
        "run-and-publish:",
    ):
        assert recipe in justfile
    assert '/Volumes/Seagate M3/projects/osm-polygon-description-tag' in justfile
    assert "NoeFlandre/osm-polygon-description-tag" in justfile
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/contracts/test_toolchain_contract.py::test_pre_commit_and_just_are_configured -q`

Expected: FAIL because both files are absent.

- [ ] **Step 3: Create the pre-commit configuration**

Use pinned hook revisions. Configure standard hygiene hooks plus Ruff's
formatter/linter, then local system-language hooks:

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
      - id: check-added-large-files
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.12.12
    hooks:
      - id: ruff-format
      - id: ruff-check
  - repo: local
    hooks:
      - id: ty
        name: ty
        entry: uv run ty check
        language: system
        pass_filenames: false
      - id: contract-tests
        name: contract tests
        entry: uv run pytest tests/contracts -q
        language: system
        pass_filenames: false
```

The Ruff hook revision is `v0.12.12`, matching the Ruff version resolved in
the current `uv.lock`. If Task 1 changes that resolved version, update both the
hook revision and this assertion in the same dependency commit.

- [ ] **Step 4: Create the Just recipes**

```just
set shell := ["bash", "-euo", "pipefail", "-c"]

sync:
    uv sync --frozen

format:
    uv run ruff format .

lint:
    uv run ruff format --check .
    uv run ruff check .

typecheck:
    uv run ty check

test:
    uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing --cov-fail-under=90

test-integration:
    uv run pytest tests/integration -q

build:
    uv build

check:
    uv lock --check
    uv run pre-commit run --all-files
    uv run ruff format --check .
    uv run ruff check .
    uv run ty check
    uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing --cov-fail-under=90
    uv build

run-and-publish:
    uv run osm-polygon-description-tag run-and-publish \
      --source-root "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
      --data-root "/Volumes/Seagate M3/projects/osm-polygon-description-tag" \
      --confirm-repo NoeFlandre/osm-polygon-description-tag
```

- [ ] **Step 5: Run GREEN without production recipe**

Run:

```bash
uv run pytest tests/contracts/test_toolchain_contract.py -q
uv run pre-commit run --all-files
just lint
just typecheck
```

Expected: PASS. Do not run `just run-and-publish`.

- [ ] **Step 6: Commit**

```bash
git add .pre-commit-config.yaml justfile tests/contracts/test_toolchain_contract.py
git commit -m "chore: add pre-commit and Just workflows"
```

### Task 6: Add the GitHub Actions quality workflow

**Files:**
- Create: `.github/workflows/quality.yml`
- Modify: `tests/contracts/test_toolchain_contract.py`

- [ ] **Step 1: Add the failing workflow contract**

```python
def test_github_actions_runs_complete_quality_gate() -> None:
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "quality.yml"
    ).read_text(encoding="utf-8")
    for token in (
        "ubuntu-latest",
        "astral-sh/setup-uv@",
        'python-version: "3.12"',
        "osmium-tool",
        "uv sync --frozen",
        "uv lock --check",
        "pre-commit run --all-files",
        "ruff format --check .",
        "ruff check .",
        "ty check",
        "--cov-fail-under=90",
        "uv build",
    ):
        assert token in workflow
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/contracts/test_toolchain_contract.py::test_github_actions_runs_complete_quality_gate -q`

Expected: FAIL because the workflow does not exist.

- [ ] **Step 3: Create the pinned workflow**

```yaml
name: quality

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  quality:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0
        with:
          version: "0.11.16"
          enable-cache: true
      - name: Install Python
        run: uv python install 3.12
      - name: Install osmium
        run: sudo apt-get update && sudo apt-get install -y osmium-tool
      - name: Sync
        run: uv sync --frozen
      - name: Verify
        run: |
          uv lock --check
          uv run pre-commit run --all-files
          uv run ruff format --check .
          uv run ruff check .
          uv run ty check
          uv run pytest --cov=osm_polygon_description_tag --cov-report=term-missing --cov-fail-under=90
          uv build
      - name: Smoke test CLI
        run: |
          uv run osm-polygon-description-tag --help
          uv run osm-polygon-description-tag run-and-publish --help
```

The action SHAs and comments above are the reviewed upstream v7.0.1 and v8.1.0
releases. Do not replace them with floating tags.

- [ ] **Step 4: Add explicit hermetic environment guards**

Set `HF_HUB_OFFLINE: "1"` and `HF_DATASETS_OFFLINE: "1"` at job level. Ensure
tests fake auth/upload subprocess boundaries; do not inject tokens or dataset
credentials.

- [ ] **Step 5: Run GREEN and validate YAML**

Run:

```bash
uv run pytest tests/contracts/test_toolchain_contract.py -q
uv run pre-commit run check-yaml --all-files
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/quality.yml tests/contracts/test_toolchain_contract.py
git commit -m "ci: add complete GitHub quality workflow"
```

### Task 7: Finalize documentation and run complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/development.md`
- Modify: `docs/architecture.md`
- Modify: `src/osm_polygon_description_tag/README.md`
- Modify: `src/osm_polygon_description_tag/runtime/README.md`
- Modify: package-layout/toolchain contracts as required

- [ ] **Step 1: Update active documentation**

Document:

- uv as authoritative dependency/command runner;
- Ruff, ty, pytest, and pre-commit gates;
- equivalent Just recipes;
- Typer ownership of the CLI;
- Rich/tqdm as interactive stderr-only presentation;
- GitHub Actions parity and synthetic osmium coverage;
- exact uv and `just run-and-publish` production commands;
- Ctrl-C 130 and same-command resumption.

Remove active argparse/mypy references, preserving historical
`docs/superpowers` records.

- [ ] **Step 2: Extend active-document contracts**

```python
def test_active_docs_name_the_complete_toolchain() -> None:
    paths = (
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "development.md",
        PROJECT_ROOT / "docs" / "architecture.md",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for name in ("uv", "Ruff", "ty", "pytest", "pre-commit", "Typer", "Rich", "tqdm", "Just", "GitHub Actions"):
        assert name in text
    assert "uv run mypy" not in text
    assert "argparse" not in text
```

- [ ] **Step 3: Run the full authoritative gate**

Run:

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

Expected: all commands exit 0, pytest has zero failures/skips, and coverage is
at least 90 percent.

- [ ] **Step 4: Verify wheel and CLI**

Run:

```bash
uv run osm-polygon-description-tag --help
uv run osm-polygon-description-tag run-and-publish --help
uv run python -c "from pathlib import Path; import zipfile; wheel=max(Path('dist').glob('*.whl'), key=lambda p:p.stat().st_mtime_ns); names=set(zipfile.ZipFile(wheel).namelist()); required={'osm_polygon_description_tag/_data/osmium-export.json','osm_polygon_description_tag/_data/dataset-card-template.md','osm_polygon_description_tag/py.typed','osm_polygon_description_tag/README.md','osm_polygon_description_tag/runtime/README.md'}; missing=required-names; assert not missing, missing"
```

Expected: both help commands exit 0 and wheel content assertion is silent.

- [ ] **Step 5: Prove no external mutation**

Compare exact pre/post `stat` output for:

```bash
stat -f '%N|%m|%z' \
  "/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw" \
  "/Volumes/Seagate M3/projects/osm-polygon-description-tag"
```

Also run:

```bash
git diff --check
git status --short --branch
```

Expected: both external records are unchanged and only intentional branch
commits differ from main.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md docs/development.md docs/architecture.md \
  src/osm_polygon_description_tag/README.md \
  src/osm_polygon_description_tag/runtime/README.md \
  tests/contracts
git commit -m "docs: document modern project workflows"
```

- [ ] **Step 7: Stop before operational execution**

Report exact verification outcomes, commit SHAs, dependency versions, command
contracts, CI/pre-commit/Just coverage, external-root integrity, and worktree
status. Do not run `just run-and-publish`, contact Hugging Face, push, merge,
or alter the production checkout without explicit authorization.
