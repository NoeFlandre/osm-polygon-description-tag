"""Contract tests for the deterministic metadata release path.

The release path must validate the complete published inventory, recompute the
card and report, publish exactly two documents plus the required visual
assets, verify the remote revision, and stay byte-stable across runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.publication import (
    REPO_ID,
    PublicationError,
    UploadItem,
    release_metadata,
    validate_published_inventory,
)
from osm_polygon_description_tag.runtime.resources import dataset_card_template
from tests.helpers.dataset import write_reporting_fixture


class _RecordingVerifier:
    """Stand-in Hub verifier returning a fixed revision."""

    def __init__(self, revision: str = "deadbeef") -> None:
        self.revision = revision
        self.calls: list[tuple[str, tuple[UploadItem, ...]]] = []

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
        self.calls.append((repo_id, files))
        return self.revision


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    data_root.mkdir()
    write_reporting_fixture(data_root, tmp_path / "raw")
    return data_root


def _release(data_root: Path, **kwargs: object) -> object:
    return release_metadata(
        data_root,
        dataset_card_template(),
        confirm_repo=kwargs.pop("confirm_repo", REPO_ID),  # type: ignore[arg-type]
        apply=bool(kwargs.pop("apply", False)),
        **kwargs,  # type: ignore[arg-type]
    )


def test_dry_run_computes_plan_without_uploading(workspace: Path) -> None:
    commands: list[list[str]] = []
    report = _release(workspace, runner=commands.append)

    assert report.repo_id == REPO_ID
    assert report.published is False
    assert report.revision is None
    assert report.validated_parquet_files == 2
    assert report.rows == 3
    assert commands == []
    assert [item.relative_path for item in report.files] == [
        "README.md",
        "stats.json",
        "assets/description_polygon_density.png",
        "assets/area_distribution.png",
        "assets/dataset-card-hero.png",
    ]


def test_apply_uploads_only_metadata_and_verifies(workspace: Path) -> None:
    commands: list[list[str]] = []
    verifier = _RecordingVerifier()

    report = _release(workspace, apply=True, runner=commands.append, verifier=verifier)

    assert report.published is True
    assert report.revision == "deadbeef"
    assert len(commands) == 1
    included = [
        command_argument
        for index, command_argument in enumerate(commands[0])
        if commands[0][index - 1] == "--include"
    ]
    assert included == [item.relative_path for item in report.files]
    assert not any(argument.startswith("data/") for argument in included)
    assert not any(argument.startswith("manifests/") for argument in included)
    assert verifier.calls == [(REPO_ID, report.files)]


def test_second_run_is_a_byte_stable_no_op(workspace: Path) -> None:
    first = _release(workspace, runner=lambda command: None)
    readme = workspace / "README.md"
    stats = workspace / "stats.json"
    before = (readme.read_bytes(), stats.read_bytes(), readme.stat().st_mtime_ns)

    second = _release(workspace, runner=lambda command: None)

    assert second.plan_identity_sha256 == first.plan_identity_sha256
    assert (readme.read_bytes(), stats.read_bytes(), readme.stat().st_mtime_ns) == before


def test_wrong_repository_confirmation_is_refused(workspace: Path) -> None:
    with pytest.raises(PublicationError, match="repository confirmation"):
        _release(workspace, confirm_repo="someone-else/osm-polygon-description-tag")


def test_missing_inventory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PublicationError, match="published data directory missing"):
        validate_published_inventory(tmp_path)


def test_empty_inventory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    with pytest.raises(PublicationError, match="no published Parquet files"):
        validate_published_inventory(tmp_path)


def test_parquet_without_matching_manifest_is_refused(workspace: Path) -> None:
    (workspace / "manifests" / "region-a.manifest.json").unlink()
    with pytest.raises(PublicationError):
        validate_published_inventory(workspace)


def test_empty_remote_revision_is_refused(workspace: Path) -> None:
    with pytest.raises(PublicationError, match="empty revision"):
        _release(
            workspace,
            apply=True,
            runner=lambda command: None,
            verifier=lambda repo_id, files: "",
        )
