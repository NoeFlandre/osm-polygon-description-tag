"""The version lives in pyproject.toml only; every other copy must match it (#73)."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import osm_polygon_description_tag

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


def test_package_version_comes_from_the_installed_metadata() -> None:
    assert osm_polygon_description_tag.__version__ == version("osm-polygon-description-tag")
    assert osm_polygon_description_tag.__version__ == _pyproject_version()


def test_citation_version_matches_the_package() -> None:
    citation = (PROJECT_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", citation, re.MULTILINE)

    assert match is not None
    assert match.group(1) == _pyproject_version()


def test_changelog_has_a_section_for_the_current_version() -> None:
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert f"## [{_pyproject_version()}]" in changelog


def test_the_release_workflow_checks_the_tag_against_the_version() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "tags:" in workflow and '"v*"' in workflow
    assert "uv build" in workflow
    assert "GITHUB_REF_NAME" in workflow
    assert "gh release create" in workflow
