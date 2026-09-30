"""Shared workspace and verifier doubles for metadata release tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from osm_polygon_description_tag.publication import REPO_ID, UploadItem, release_metadata
from osm_polygon_description_tag.runtime.resources import dataset_card_template
from tests.helpers.dataset import write_reporting_fixture


class RecordingVerifier:
    """Record release verification calls while returning stable revisions."""

    def __init__(self, revision: str = "deadbeef") -> None:
        self.revision = revision
        self.calls: list[tuple[str, tuple[UploadItem, ...]]] = []
        self.inventory_calls: list[tuple[str, tuple[UploadItem, ...], str | None]] = []
        self.matching_calls: list[tuple[str, tuple[UploadItem, ...]]] = []
        self.remote_metadata_revision: str | None = None

    def __call__(self, repo_id: str, files: tuple[UploadItem, ...]) -> str:
        self.calls.append((repo_id, files))
        return self.revision

    def verify_inventory(
        self,
        repo_id: str,
        files: tuple[UploadItem, ...],
        *,
        revision: str | None = None,
    ) -> str:
        self.inventory_calls.append((repo_id, files, revision))
        return revision or "data-revision"

    def matching_revision(self, repo_id: str, files: tuple[UploadItem, ...]) -> str | None:
        self.matching_calls.append((repo_id, files))
        return self.remote_metadata_revision


@pytest.fixture(name="workspace")
def release_workspace(tmp_path: Path) -> Path:
    data_root = tmp_path / "generated"
    data_root.mkdir()
    write_reporting_fixture(data_root, tmp_path / "raw")
    return data_root


def release(data_root: Path, **kwargs: object) -> object:
    return release_metadata(
        data_root,
        dataset_card_template(),
        confirm_repo=kwargs.pop("confirm_repo", REPO_ID),  # type: ignore[arg-type]
        apply=bool(kwargs.pop("apply", False)),
        **kwargs,  # type: ignore[arg-type]
    )
