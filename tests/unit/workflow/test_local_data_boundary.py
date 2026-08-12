"""All generated state must stay below the configured data root."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.config import Paths
from osm_polygon_description_tag.publication import UploadItem
from osm_polygon_description_tag.workflow.orchestrator import (
    default_hub_verifier_factory,
    run_and_publish,
)


def test_denied_preflight_does_not_create_logs(tmp_path: Path) -> None:
    source_root = tmp_path / "raw"
    data_root = tmp_path / "generated"
    source_root.mkdir()
    data_root.mkdir()
    before = tuple(data_root.iterdir())

    def deny() -> dict[str, object]:
        raise RuntimeError("denied")

    with pytest.raises(RuntimeError, match="denied"):
        run_and_publish(
            paths=Paths(source_root=source_root, data_root=data_root),
            confirm_repo="NoeFlandre/osm-polygon-description-tag",
            preflight=deny,
        )

    assert tuple(data_root.iterdir()) == before


def test_hub_verifier_download_cache_is_below_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "generated"
    cache_dir = data_root / ".cache" / "huggingface" / "hub"
    content = b"remote"
    artifact = tmp_path / "artifact"
    artifact.write_bytes(content)
    seen: dict[str, object] = {}

    class Api:
        def whoami(self) -> dict[str, str]:
            return {"name": "tester"}

        def repo_info(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(sha="revision")

        def get_paths_info(self, *args: object, **kwargs: object) -> list[SimpleNamespace]:
            return [SimpleNamespace(size=len(content), lfs=None)]

        def hf_hub_download(self, *args: object, **kwargs: object) -> str:
            seen.update(kwargs)
            return str(artifact)

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight._huggingface_hub.HfApi",
        Api,
    )
    import hashlib

    verifier = default_hub_verifier_factory(cache_dir=cache_dir)
    item = UploadItem(
        relative_path="README.md",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    assert verifier("NoeFlandre/osm-polygon-description-tag", (item,)) == "revision"
    assert seen["cache_dir"] == cache_dir


def test_validation_index_is_created_below_data_root(tmp_path: Path) -> None:
    from osm_polygon_description_tag.dataset.storage import _UniquenessIndex

    data_root = tmp_path / "generated"
    work_root = data_root / ".work" / "validation"
    with _UniquenessIndex(work_root=work_root) as index:
        assert index.db_path.is_relative_to(data_root)
        assert index.db_path.is_file()
    assert not work_root.exists()


def test_reporting_spill_directory_is_below_data_root(tmp_path: Path) -> None:
    from osm_polygon_description_tag.dataset.stats import _new_connection

    data_root = tmp_path / "generated"
    connection = _new_connection(data_root)
    try:
        configured = connection.execute("SELECT current_setting('temp_directory')").fetchone()
        assert configured is not None
        assert Path(str(configured[0])).is_relative_to(data_root)
    finally:
        connection.close()


def test_default_verifier_reconciles_only_stale_managed_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []

    class Api:
        def list_repo_files(self, *args: object, **kwargs: object) -> list[str]:
            return [
                ".gitattributes",
                "README.md",
                "data/current.parquet",
                "data/stale.parquet",
                "manifests/current.manifest.json",
                "manifests/stale.manifest.json",
                "notes/preserve.txt",
            ]

        def delete_files(
            self,
            _repo_id: str,
            delete_patterns: list[str],
            **kwargs: object,
        ) -> SimpleNamespace:
            deleted.extend(delete_patterns)
            return SimpleNamespace(oid="revision-after-delete")

    monkeypatch.setattr(
        "osm_polygon_description_tag.workflow.preflight._huggingface_hub.HfApi",
        Api,
    )
    verifier = default_hub_verifier_factory()
    reconcile = verifier.reconcile_managed_files  # type: ignore[attr-defined]
    revision = reconcile(
        "NoeFlandre/osm-polygon-description-tag",
        {
            "README.md",
            "data/current.parquet",
            "manifests/current.manifest.json",
            "stats.json",
        },
    )
    assert deleted == [
        "data/stale.parquet",
        "manifests/stale.manifest.json",
    ]
    assert revision == "revision-after-delete"
