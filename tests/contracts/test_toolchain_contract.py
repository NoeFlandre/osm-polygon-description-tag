from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _requirements_by_name(requirements: list[str]) -> dict[str, Requirement]:
    parsed = (Requirement(requirement) for requirement in requirements)
    return {requirement.name.lower(): requirement for requirement in parsed}


def test_runtime_and_development_dependencies_use_the_standard_toolchain() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    runtime = _requirements_by_name(project["project"]["dependencies"])
    development = _requirements_by_name(project["dependency-groups"]["dev"])

    assert str(runtime["typer"].specifier) == "<1,>=0.26"
    assert str(runtime["rich"].specifier) == "<15,>=13"
    assert str(runtime["tqdm"].specifier) == "<5,>=4.66"
    assert {"ruff", "ty", "pytest", "pytest-cov", "pre-commit"} <= development.keys()
    assert "mypy" not in development


def test_ty_configuration_checks_the_src_package_strictly() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["tool"]["ty"]["environment"] == {
        "python-version": "3.12",
        "root": ["./src", "."],
    }
    # scripts/ holds the CI gate logic, so it is type-checked too (#72).
    assert project["tool"]["ty"]["src"] == {"include": ["src", "scripts"]}
    assert project["tool"]["ty"]["terminal"] == {"error-on-warning": True}
    assert "mypy" not in project["tool"]


def test_typer_fully_owns_the_cli() -> None:
    cli_source = (PROJECT_ROOT / "src" / "osm_polygon_description_tag" / "cli.py").read_text(
        encoding="utf-8"
    )
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "import argparse" not in cli_source
    assert "typer.Typer" in cli_source
    assert (
        project["project"]["scripts"]["osm-polygon-description-tag"]
        == "osm_polygon_description_tag.cli:main"
    )


def test_pre_commit_and_just_are_configured() -> None:
    pre_commit = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    justfile = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")

    for token in ("ruff-format", "ruff-check", "uv run ty check", "uv run pytest"):
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
        "run-and-publish ",
    ):
        assert recipe in justfile
    # Roots come from the environment or pass-through options, never the author's machine.
    assert "/Volumes" not in justfile
    assert "run-and-publish *args:" in justfile
    assert "NoeFlandre/osm-polygon-description-tag" in justfile


def test_github_actions_runs_complete_quality_gate() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    for token in (
        "ubuntu-latest",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
        'version: "0.11.16"',
        "uv python install 3.12",
        "osmium-tool",
        "uv sync --frozen",
        "uv lock --check",
        "pre-commit run --all-files",
        "ruff format --check .",
        "ruff check .",
        "ty check",
        "uv build",
        'HF_HUB_OFFLINE: "1"',
    ):
        assert token in workflow


def test_the_coverage_threshold_is_defined_once_in_pyproject() -> None:
    import tomllib

    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files = [
        PROJECT_ROOT / "justfile",
        *sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml")),
    ]

    assert config["tool"]["coverage"]["report"]["fail_under"] >= 90
    assert [f.name for f in files if "--cov-fail-under" in f.read_text(encoding="utf-8")] == []


def test_public_docs_name_no_author_machine_paths() -> None:
    """Only the labelled maintainer example in operations.md may name /Volumes (#67)."""
    docs = [PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md"))]
    docs += sorted((PROJECT_ROOT / "src").rglob("README.md"))
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in docs
        if "/Volumes" in path.read_text(encoding="utf-8")
    ]

    assert offenders == ["docs/operations.md"]
    operations = (PROJECT_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    example = operations.split("### Maintainer setup (example)", 1)
    assert len(example) == 2
    assert "/Volumes" not in example[0]


def test_dependabot_updates_every_pinned_ecosystem() -> None:
    import yaml

    config = yaml.safe_load((PROJECT_ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}

    assert ecosystems == {"github-actions", "docker", "uv", "pre-commit"}
    assert all(update["schedule"]["interval"] == "weekly" for update in config["updates"])
    assert all(update.get("groups") for update in config["updates"])


def test_dev_language_pins_equal_the_language_extra() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = set(project["project"]["optional-dependencies"]["language"])
    dev = project["dependency-groups"]["dev"]

    shared = [pin for pin in dev if pin.startswith(("lingua-language-detector", "fasttext-numpy2"))]
    assert len(shared) == 2
    assert set(shared) <= extra


def test_pre_release_tools_are_bounded_and_ruff_is_pinned_once() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = project["dependency-groups"]["dev"]
    pre_commit = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "ty>=0.0.65,<0.0.66" in dev
    assert "pre-commit>=4.6.1,<5" in dev
    assert "ruff-pre-commit" not in pre_commit
    assert "entry: uv run ruff check --fix" in pre_commit
