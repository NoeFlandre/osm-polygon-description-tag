"""Tests for the packaged resource resolver and code revision lookup."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from osm_polygon_description_tag.runtime.resources import (
    _read_git_revision,
    _revision_from_process,
    dataset_card_hero,
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


def test_package_data_dir_joins_the_canonical_data_subdirectory(
    tmp_path: Path,
) -> None:
    joined = tmp_path / "_data"
    package_files = Mock()
    package_files.joinpath.return_value = joined

    with patch("importlib.resources.files", return_value=package_files) as files:
        assert package_data_dir() == joined

    files.assert_called_once_with("osm_polygon_description_tag")
    package_files.joinpath.assert_called_once_with("_data")


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


def test_dataset_card_hero_is_packaged() -> None:
    path = dataset_card_hero()
    assert path.is_file()
    assert path == package_data_dir() / "dataset-card-hero.png"


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


def test_project_root_rejects_case_variant_pyproject_in_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    sentinel = tmp_path / "package" / "resources.py"
    monkeypatch.setattr(resources, "__file__", str(sentinel))
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", str(fake_root))

    def case_sensitive_is_file(path: Path) -> bool:
        return path == fake_root / "PYPROJECT.TOML"

    monkeypatch.setattr(Path, "is_file", case_sensitive_is_file)
    with pytest.raises(FileNotFoundError, match="project root"):
        resources.project_root()


def test_project_root_rejects_case_variant_pyproject_during_upward_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    sentinel = fake_root / "package" / "resources.py"
    monkeypatch.setattr(resources, "__file__", str(sentinel))
    monkeypatch.delenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", raising=False)

    def case_sensitive_is_file(path: Path) -> bool:
        return path == fake_root / "PYPROJECT.TOML"

    monkeypatch.setattr(Path, "is_file", case_sensitive_is_file)
    with pytest.raises(FileNotFoundError, match="project root"):
        resources.project_root()


def test_project_root_ignores_dangling_environment_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    missing = tmp_path / "missing-project"
    fake_root = tmp_path / "project-link"
    try:
        fake_root.symlink_to(missing, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    sentinel = tmp_path / "package" / "resources.py"
    monkeypatch.setattr(resources, "__file__", str(sentinel))
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", str(fake_root))

    with pytest.raises(FileNotFoundError) as error:
        resources.project_root()
    assert str(error.value) == "project root with pyproject.toml not found"


def test_project_root_resolves_environment_path_non_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    (fake_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    expected_root = fake_root.resolve(strict=False)
    monkeypatch.setenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", str(fake_root))
    calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []
    real_resolve = Path.resolve

    def record_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        calls.append((path, args, kwargs))
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)

    assert resources.project_root() == expected_root
    assert calls == [(Path(str(fake_root)), (), {"strict": False})]


def test_project_root_resolves_package_path_non_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    sentinel = tmp_path / "package" / "resources.py"
    monkeypatch.setattr(resources, "__file__", str(sentinel))
    monkeypatch.delenv("OSM_POLYGON_DESCRIPTION_TAG_HOME", raising=False)
    calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []
    real_resolve = Path.resolve

    def record_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        calls.append((path, args, kwargs))
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)
    with pytest.raises(FileNotFoundError, match="project root"):
        resources.project_root()

    assert calls == [(Path(str(sentinel)), (), {"strict": False})]


def test_read_git_revision_uses_exact_bounded_command(tmp_path: Path) -> None:
    completed = Mock(returncode=0, stdout="abcdef\n")
    with patch(
        "osm_polygon_description_tag.runtime.resources.subprocess.run",
        return_value=completed,
    ) as run:
        assert _read_git_revision(tmp_path, "/custom/git") == "abcdef"

    run.assert_called_once_with(
        ["/custom/git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    ((0, "abcdef\n", "abcdef"), (0, " \n", None), (1, "abcdef\n", None)),
)
def test_revision_from_process_accepts_only_successful_nonempty_revision(
    returncode: int, stdout: str, expected: str | None
) -> None:
    assert _revision_from_process(returncode, stdout) == expected


def test_project_code_revision_forwards_root_and_git_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    with (
        patch.object(resources, "project_root", return_value=tmp_path) as root,
        patch.object(resources, "_read_git_revision", return_value="abcdef") as read,
        patch.object(shutil, "which", return_value=None) as which,
    ):
        assert resources.project_code_revision() == "abcdef"

    root.assert_called_once_with()
    which.assert_called_once_with("git")
    read.assert_called_once_with(tmp_path, "git")


def test_project_code_revision_uses_discovered_git_executable(
    tmp_path: Path,
) -> None:
    import osm_polygon_description_tag.runtime.resources as resources

    with (
        patch.object(resources, "project_root", return_value=tmp_path),
        patch.object(resources, "_read_git_revision", return_value="abcdef") as read,
        patch.object(shutil, "which", return_value="/opt/bin/git") as which,
    ):
        assert resources.project_code_revision() == "abcdef"

    which.assert_called_once_with("git")
    read.assert_called_once_with(tmp_path, "/opt/bin/git")


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
    from osm_polygon_description_tag.cli import run

    cwd_before = os.getcwd()
    try:
        os.chdir(tmp_path)
        exit_code = run(["run-and-publish", "--help"])
        assert exit_code == 0
        assert osmium_export_config().is_file()
        assert dataset_card_template().is_file()
    finally:
        os.chdir(cwd_before)


def test_resource_path_rejects_missing_resource() -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        resource_path("does-not-exist.json")
