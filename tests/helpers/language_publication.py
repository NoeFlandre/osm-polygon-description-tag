"""Shared source/export/Hub fixtures for language publication contracts."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from pathlib import Path

import pytest

from osm_polygon_description_tag.dataset.languages.models import (
    LanguageResult,
    LanguageStatus,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    prepare_snapshot,
)
from osm_polygon_description_tag.dataset.languages.worker import process_shard
from osm_polygon_description_tag.publication.language import (
    LANGUAGE_CONFIG_NAME,
    LanguageExport,
    export_language_annotations,
)
from osm_polygon_description_tag.publication.language_upload import RemoteFile
from osm_polygon_description_tag.publication.models import UploadPlan
from tests.helpers.parquet import write_description_shard
from tests.helpers.sentences import fake_splitter

REPO = "NoeFlandre/osm-polygon-description-tag"
SHARD = "region.parquet"


def detector(text: str) -> LanguageResult:
    if text.startswith("Le "):
        return LanguageResult("fra", 0.95, 0.05, 0.9, LanguageStatus.DETECTED, "detected")
    if text.startswith("?"):
        return LanguageResult(None, 0.4, 0.3, 0.1, LanguageStatus.UNCERTAIN, "low_confidence")
    return LanguageResult("eng", 0.9, 0.1, 0.8, LanguageStatus.DETECTED, "detected")


def tags(index: int) -> dict[str, str]:
    values = {"description": f"A synthetic description {index}"}
    if index % 2 == 0:
        values["description:fr"] = f"Le mur {index}"
    if index % 3 == 0:
        values["description:zz"] = "?"
    return values


write_shard = partial(write_description_shard, batch_size=3, tags=tags)


@pytest.fixture(name="run_dir")
def run_directory(tmp_path: Path) -> tuple[Path, Path, SnapshotManifest]:
    source = tmp_path / "source"
    write_shard(source / SHARD, 6)
    run = tmp_path / "run"
    snapshot = prepare_snapshot(source, run, code_fingerprint="a" * 64, lock_fingerprint="b" * 64)
    return source, run, snapshot


def process(source: Path, run: Path, snapshot: SnapshotManifest, shard: str = SHARD) -> None:
    process_shard(
        run,
        source,
        shard,
        detector=detector,
        splitter=fake_splitter(),
        snapshot=snapshot,
        batch_size=3,
    )


@pytest.fixture(name="export")
def language_export(run_dir: tuple[Path, Path, SnapshotManifest], tmp_path: Path) -> LanguageExport:
    source, run, snapshot = run_dir
    process(source, run, snapshot)
    return export_language_annotations(run, tmp_path / "export")


class FakeHub:
    """An in-memory Hub that records files and simulates verification outcomes."""

    def __init__(
        self,
        *,
        revisions: list[str] | None = None,
        configs: tuple[str, ...] = (LANGUAGE_CONFIG_NAME,),
        fail_upload: bool = False,
        corrupt: str | None = None,
        missing: str | None = None,
    ) -> None:
        self._revisions = revisions or ["rev-1", "rev-1"]
        self._configs = configs
        self._fail_upload = fail_upload
        self._corrupt = corrupt
        self._missing = missing
        self.uploads: list[UploadPlan] = []
        self.stored: dict[str, RemoteFile] = {}

    def repo_revision(self, repo_id: str) -> str:
        return self._revisions.pop(0) if len(self._revisions) > 1 else self._revisions[0]

    def upload(self, plan: UploadPlan, *, parent_revision: str | None = None) -> None:
        if self._fail_upload:
            raise RuntimeError("network died mid-upload")
        self.uploads.append(plan)
        for item in plan.files:
            size = item.size_bytes + (1 if item.relative_path == self._corrupt else 0)
            self.stored[item.relative_path] = RemoteFile(item.relative_path, size, item.sha256)

    def paths_info(
        self, repo_id: str, revision: str, paths: Sequence[str]
    ) -> tuple[RemoteFile, ...]:
        return tuple(
            self.stored[path] for path in paths if path in self.stored and path != self._missing
        )

    def dataset_configs(self, repo_id: str, revision: str) -> tuple[str, ...]:
        return self._configs
