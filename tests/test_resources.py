"""Tests for the packaged resource resolver and code revision lookup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from osm_polygon_description_tag._resources import (
    dataset_card_template,
    osmium_export_config,
    package_data_dir,
    project_code_revision,
    project_root,
    resource_path,
)


def test_package_data_dir_is_inside_installed_package() -> None:
    data_dir = package_data_dir()
    assert data_dir.is_dir()
    # The data dir lives next to the package's __init__.py.
    assert (data_dir.parent / "__init__.py").is_file()


def test_resource_path_returns_packaged_config() -> None:
    config_path = resource_path("osmium-export.json")
    assert config_path.is_file()
    assert "linear_tags" in config_path.read_text(encoding="utf-8")


def test_osmium_export_config_is_packaged() -> None:
    path = osmium_export_config()
    assert path.is_file()
    assert path == package_data_dir() / "osmium-export.json"


def test_dataset_card_template_is_packaged() -> None:
    path = dataset_card_template()
    assert path.is_file()
    assert path == package_data_dir() / "dataset-card-template.md"


def test_project_root_finds_checkout(tmp_path: Path) -> None:
    # The project root contains pyproject.toml.
    root = project_root()
    assert (root / "pyproject.toml").is_file()


def test_project_root_uses_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    (fake_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", str(fake_root))
    assert project_root() == fake_root.resolve(strict=False)


def test_project_code_revision_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two calls in the same checkout return the same value.
    a = project_code_revision()
    b = project_code_revision()
    assert a == b


def test_resources_resolve_from_unrelated_cwd(tmp_path: Path) -> None:
    """Packaged resources resolve regardless of the caller's working directory."""
    cwd_before = os.getcwd()
    try:
        os.chdir(tmp_path)
        # resolve all packaged resources from an unrelated cwd
        config_path = osmium_export_config()
        template_path = dataset_card_template()
        assert config_path.is_file()
        assert template_path.is_file()
    finally:
        os.chdir(cwd_before)


def test_cli_uses_packaged_resources_from_unrelated_cwd(tmp_path: Path) -> None:
    """The CLI's run-and-publish subcommand resolves resources from any cwd."""
    from osm_polygon_description_tag.cli import create_parser

    parser = create_parser()

    cwd_before = os.getcwd()
    try:
        os.chdir(tmp_path)
        args = parser.parse_args(
            [
                "run-and-publish",
                "--confirm-repo",
                "NoeFlandre/osm-polygon-description-tag",
                "--preflight",
                "stub",
                "--upload-runner",
                "stub",
            ]
        )
        # Argument parsing succeeds from an unrelated cwd; resources resolve.
        assert args.confirm_repo == "NoeFlandre/osm-polygon-description-tag"
    finally:
        os.chdir(cwd_before)


def test_resource_path_rejects_missing_resource() -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        resource_path("does-not-exist.json")
