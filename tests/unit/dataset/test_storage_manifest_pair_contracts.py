"""Contracts for finalized-artifact manifest pairing."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset import storage_artifacts, storage_validation
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    RunCounts,
    SourceIdentity,
    manifest_path_for,
    output_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage_errors import StorageError


def _write_finalized_pair(
    data_root: Path, manifest_factory, *, rows: int, included_rows: int
) -> None:
    """Write a Parquet with ``rows`` rows and a manifest that records ``included_rows``."""
    data_dir = data_root / "data"
    data_dir.mkdir(parents=True)
    (data_root / "manifests").mkdir()
    parquet = data_dir / "region.parquet"
    pq.write_table(pa.table({"description": ["text"] * rows}), parquet)
    manifest: Manifest = manifest_factory(
        source=SourceIdentity("region.osm.pbf", 1, 1, "a" * 64),
        output=output_identity_for(parquet),
    )
    counts = RunCounts(included_rows, included_rows, {})
    write_manifest(
        replace(manifest, counts=counts),
        manifest_path_for(parquet.name, data_root),
    )


@pytest.mark.parametrize(
    ("rows", "included_rows"),
    [(1, 2), (2, 3)],
    ids=["issue-160-stale-count", "overstated-count"],
)
def test_validation_rejects_a_manifest_whose_included_rows_disagree_with_the_parquet(
    tmp_path: Path, manifest_factory, rows: int, included_rows: int
) -> None:
    _write_finalized_pair(tmp_path, manifest_factory, rows=rows, included_rows=included_rows)

    with pytest.raises(StorageError) as error:
        storage_artifacts.validate_finalized_artifacts(tmp_path)

    assert str(error.value) == (
        f"manifest row count mismatch for region.parquet: recorded {included_rows}, found {rows}"
    )


def test_validation_accepts_a_manifest_whose_included_rows_match_the_parquet(
    tmp_path: Path, manifest_factory
) -> None:
    _write_finalized_pair(tmp_path, manifest_factory, rows=2, included_rows=2)

    result = storage_artifacts.validate_finalized_artifacts(tmp_path)

    assert result["parquets"] == (tmp_path / "data" / "region.parquet",)


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
        manifest_schema_version=storage_artifacts.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )

    with (
        patch.object(storage_artifacts, "read_manifest", return_value=manifest),
        patch.object(storage_artifacts, "output_identity_for", return_value=identity),
        patch.object(storage_artifacts, "is_resumable") as is_resumable,
        patch.object(storage_artifacts, "_validate_manifest_included_rows"),
    ):
        pair = storage_artifacts._validate_manifest_pair_record(parquet, manifests_dir)

    assert pair.path == manifest_path
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
        manifest_schema_version=storage_artifacts.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )

    with (
        patch.object(storage_artifacts, "read_manifest", return_value=manifest),
        patch.object(storage_artifacts, "output_identity_for", return_value=identity),
        patch.object(storage_artifacts, "is_resumable", return_value=False) as is_resumable,
        pytest.raises(
            StorageError, match="manifest contract does not match current configuration"
        ) as error,
    ):
        storage_artifacts._validate_manifest_pair(
            parquet, manifests_dir, require_current_contract=True
        )

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
        storage_artifacts.validate_finalized_artifacts(data_root)

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
        storage_artifacts.validate_finalized_artifacts(data_root)

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
        storage_artifacts.validate_finalized_artifacts(data_root)


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
        storage_artifacts._validate_manifest_pair(parquet, manifests_dir)

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
        storage_artifacts._validate_manifest_pair(parquet, manifests_dir)

    assert str(error.value) == f"cannot inspect manifest {manifest_path}: permission denied"


def test_validate_manifest_pair_preserves_output_read_context(tmp_path: Path) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "region.manifest.json").write_bytes(b"tiny manifest fixture")
    manifest = SimpleNamespace(
        manifest_schema_version=storage_artifacts.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=object(),
    )

    with (
        patch.object(storage_artifacts, "read_manifest", return_value=manifest),
        patch.object(storage_artifacts, "output_identity_for", side_effect=OSError("disk error")),
        pytest.raises(StorageError) as error,
    ):
        storage_artifacts._validate_manifest_pair(parquet, manifests_dir)

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
        manifest_schema_version=storage_artifacts.MANIFEST_SCHEMA_VERSION,
        source=object(),
        output=identity,
    )
    lookup_calls: list[tuple[str, Path | None]] = []

    def resolve_manifest(output_name: str, data_root: Path | None) -> Path:
        lookup_calls.append((output_name, data_root))
        return manifest_path

    monkeypatch.setattr(storage_artifacts, "manifest_path_for", resolve_manifest)
    with (
        patch.object(storage_artifacts, "read_manifest", return_value=manifest),
        patch.object(storage_artifacts, "output_identity_for", return_value=identity),
        patch.object(storage_artifacts, "_validate_manifest_included_rows"),
    ):
        result = storage_artifacts._validate_manifest_pair(parquet, manifests_dir)

    assert result == manifest_path
    assert lookup_calls == [(parquet.name, tmp_path)]


def test_validate_source_reports_the_expected_manifest_identity() -> None:
    state = SimpleNamespace(source_pbf=None)

    with pytest.raises(StorageError) as error:
        storage_validation._validate_source(
            state, current="actual.osm.pbf", expected_source_pbf="recorded.osm.pbf"
        )

    assert str(error.value) == (
        "manifest source identity mismatch: expected 'recorded.osm.pbf', found 'actual.osm.pbf'"
    )
