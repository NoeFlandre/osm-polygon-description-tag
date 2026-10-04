"""Contracts for finalized-artifact manifest pairing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osm_polygon_description_tag.dataset import storage
from osm_polygon_description_tag.dataset.storage import StorageError


def test_validate_manifest_pair_default_does_not_require_current_contract(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"
    manifest_path.write_bytes(b"tiny manifest fixture")
    identity = object()
    manifest = SimpleNamespace(
        manifest_schema_version=storage.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )

    with (
        patch.object(storage, "read_manifest", return_value=manifest),
        patch.object(storage, "output_identity_for", return_value=identity),
        patch.object(storage, "is_resumable") as is_resumable,
    ):
        assert storage._validate_manifest_pair(parquet, manifests_dir) == manifest_path

    is_resumable.assert_not_called()


def test_validate_manifest_pair_enforces_current_contract_when_requested(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "region.manifest.json").write_bytes(b"tiny manifest fixture")
    identity = object()
    manifest = SimpleNamespace(
        manifest_schema_version=storage.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )

    with (
        patch.object(storage, "read_manifest", return_value=manifest),
        patch.object(storage, "output_identity_for", return_value=identity),
        patch.object(storage, "is_resumable", return_value=False) as is_resumable,
        pytest.raises(
            StorageError, match="manifest contract does not match current configuration"
        ) as error,
    ):
        storage._validate_manifest_pair(parquet, manifests_dir, require_current_contract=True)

    is_resumable.assert_called_once_with(manifest, manifest.source, identity)
    assert str(manifests_dir / "region.manifest.json") in str(error.value)


def test_validate_manifest_pair_names_a_non_regular_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    parquet = data_dir / "region.parquet"
    parquet.mkdir()
    manifests_dir = data_root / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "region.manifest.json").write_bytes(b"tiny manifest fixture")

    with pytest.raises(StorageError) as error:
        storage.validate_finalized_artifacts(data_root)

    assert str(error.value) == f"finalized artifact is not a regular file: {parquet}"


def test_validate_manifest_pair_names_a_non_regular_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "generated"
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    parquet = data_dir / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = data_root / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"
    manifest_path.mkdir()

    with pytest.raises(StorageError) as error:
        storage.validate_finalized_artifacts(data_root)

    assert str(error.value) == f"manifest is not a regular file: {manifest_path}"


def test_validate_finalized_artifacts_rejects_a_symlinked_data_root(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-root"
    (actual_root / "data").mkdir(parents=True)
    (actual_root / "manifests").mkdir()
    data_root = tmp_path / "data-root"
    try:
        data_root.symlink_to(actual_root, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support directory symlinks")

    with pytest.raises(StorageError, match="data root is not a regular directory"):
        storage.validate_finalized_artifacts(data_root)


def test_validate_manifest_pair_preserves_artifact_inspection_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "region.manifest.json").write_bytes(b"tiny manifest fixture")
    original_is_symlink = Path.is_symlink

    def fail_for_parquet(path: Path) -> bool:
        if path == parquet:
            raise OSError("permission denied")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fail_for_parquet)

    with pytest.raises(StorageError) as error:
        storage._validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == f"cannot inspect finalized artifact {parquet}: permission denied"


def test_validate_manifest_pair_preserves_manifest_inspection_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"
    manifest_path.write_bytes(b"tiny manifest fixture")
    original_is_symlink = Path.is_symlink

    def fail_for_manifest(path: Path) -> bool:
        if path == manifest_path:
            raise OSError("permission denied")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fail_for_manifest)

    with pytest.raises(StorageError) as error:
        storage._validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == f"cannot inspect manifest {manifest_path}: permission denied"


def test_validate_manifest_pair_preserves_output_read_context(tmp_path: Path) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "region.manifest.json").write_bytes(b"tiny manifest fixture")
    manifest = SimpleNamespace(
        manifest_schema_version=storage.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=object(),
    )

    with (
        patch.object(storage, "read_manifest", return_value=manifest),
        patch.object(storage, "output_identity_for", side_effect=OSError("disk error")),
        pytest.raises(StorageError) as error,
    ):
        storage._validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == f"cannot read finalized artifact {parquet}: disk error"


def test_validate_manifest_pair_uses_the_data_root_for_manifest_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    parquet = data_dir / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    manifest_path = manifests_dir / "region.manifest.json"
    manifest_path.write_bytes(b"tiny manifest fixture")
    identity = object()
    manifest = SimpleNamespace(
        manifest_schema_version=storage.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )
    lookup_calls: list[tuple[str, Path | None]] = []

    def resolve_manifest(output_name: str, data_root: Path | None) -> Path:
        lookup_calls.append((output_name, data_root))
        return manifest_path

    monkeypatch.setattr(storage, "_manifest_path_for", resolve_manifest)
    with (
        patch.object(storage, "read_manifest", return_value=manifest),
        patch.object(storage, "output_identity_for", return_value=identity),
    ):
        result = storage._validate_manifest_pair(parquet, manifests_dir)

    assert result == manifest_path
    assert lookup_calls == [(parquet.name, tmp_path)]


def test_validate_source_reports_the_expected_manifest_identity() -> None:
    state = SimpleNamespace(source_pbf=None)

    with pytest.raises(StorageError) as error:
        storage._validate_source(
            state, current="actual.osm.pbf", expected_source_pbf="recorded.osm.pbf"
        )

    assert str(error.value) == (
        "manifest source identity mismatch: expected 'recorded.osm.pbf', found 'actual.osm.pbf'"
    )
