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
