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

    assert str(runtime["typer"].specifier) == "<1,>=0.12"
    assert str(runtime["rich"].specifier) == "<15,>=13"
    assert str(runtime["tqdm"].specifier) == "<5,>=4.66"
    assert {"ruff", "ty", "pytest", "pytest-cov", "pre-commit"} <= development.keys()
    assert "mypy" not in development


def test_ty_configuration_checks_the_src_package_strictly() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["tool"]["ty"]["environment"] == {
        "python-version": "3.12",
        "root": ["./src"],
    }
    assert project["tool"]["ty"]["src"] == {"include": ["src"]}
    assert project["tool"]["ty"]["terminal"] == {"error-on-warning": True}
    assert "mypy" not in project["tool"]


def test_lockfile_has_no_mypy_package() -> None:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))

    locked_names = {package["name"].lower() for package in lock["package"]}
    assert "mypy" not in locked_names


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
        "run-and-publish:",
    ):
        assert recipe in justfile
    assert '"/Volumes/Seagate M3/projects/osm-polygon-wikidata-only/raw"' in justfile
    assert '"/Volumes/Seagate M3/projects/osm-polygon-description-tag"' in justfile
    assert "NoeFlandre/osm-polygon-description-tag" in justfile


def test_github_actions_runs_complete_quality_gate() -> None:
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "quality.yml"
    ).read_text(encoding="utf-8")

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
        "--cov-fail-under=90",
        "uv build",
        'HF_HUB_OFFLINE: "1"',
    ):
        assert token in workflow
