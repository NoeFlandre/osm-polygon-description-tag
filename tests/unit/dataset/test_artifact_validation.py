"""Direct contracts for validating finalized dataset outputs.

The ``validate`` command is a thin wrapper over these functions. Each test here
calls one of them without going through Typer, and pins what a caller can
observe: the value it returns, or the message and type of the error it raises.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_description_tag.dataset import validation
from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    manifest_path_for,
    output_identity_for,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.storage import StorageError, write_geoparquet


def _write_source(source_root: Path, name: str = "region.osm.pbf") -> Path:
    source_root.mkdir(parents=True, exist_ok=True)
    source = source_root / name
    source.write_bytes(b"tiny source fixture")
    return source


def test_require_regular_source_file_returns_a_plain_file(tmp_path: Path) -> None:
    source = _write_source(tmp_path)

    assert validation.require_regular_source_file(source) == source


def test_require_regular_source_file_rejects_a_directory_with_the_identity_message(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "region.osm.pbf"
    directory.mkdir()

    with pytest.raises(StorageError) as error:
        validation.require_regular_source_file(directory)

    assert str(error.value) == (
        f"source identity mismatch: missing regular source file {directory}"
    )


def test_require_regular_source_file_rejects_a_symlink_to_a_real_file(tmp_path: Path) -> None:
    target = _write_source(tmp_path / "raw", "target.osm.pbf")
    link = tmp_path / "raw" / "linked.osm.pbf"
    link.symlink_to(target)

    with pytest.raises(StorageError, match="missing regular source file"):
        validation.require_regular_source_file(link)


def test_read_source_identity_matches_the_manifest_identity_function(tmp_path: Path) -> None:
    source = _write_source(tmp_path)

    assert validation.read_source_identity(source) == source_identity_for(source)


def test_validate_source_file_identity_accepts_the_bytes_the_manifest_recorded(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path)
    manifest = SimpleNamespace(source=source_identity_for(source))

    assert validation.validate_source_file_identity(manifest, tmp_path) is None


def test_validate_source_file_identity_rejects_bytes_that_changed_after_the_manifest(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path)
    manifest = SimpleNamespace(source=source_identity_for(source))
    source.write_bytes(b"a different source, same name")

    with pytest.raises(StorageError) as error:
        validation.validate_source_file_identity(manifest, tmp_path)

    assert str(error.value) == "source identity mismatch for region.osm.pbf"


def test_validate_dataset_outputs_reports_a_missing_data_directory(tmp_path: Path) -> None:
    with pytest.raises(StorageError) as error:
        validation.validate_dataset_outputs(tmp_path, None)

    assert str(error.value) == f"missing data directory: {tmp_path / 'data'}"


@pytest.fixture
def artifact_pair(
    tmp_path: Path,
    manifest_factory: Callable[..., Manifest],
    valid_records: list[dict[str, object]],
) -> tuple[Path, Path, Path]:
    """One finalized artifact and its manifest, written through the real storage code."""
    data_root = tmp_path / "generated"
    (data_root / "data").mkdir(parents=True)
    (data_root / "manifests").mkdir()
    source_root = tmp_path / "raw"
    source = _write_source(source_root, "region.osm.pbf")
    parquet = data_root / "data" / "region.parquet"
    write_geoparquet([dict(valid_records[0], source_pbf=source.name)], parquet)
    write_manifest(
        manifest_factory(source=source_identity_for(source), output=output_identity_for(parquet)),
        manifest_path_for(parquet.name, data_root),
    )
    return data_root, source_root, parquet


def test_validate_dataset_outputs_summarises_each_verified_pair(
    artifact_pair: tuple[Path, Path, Path],
) -> None:
    data_root, _source_root, _parquet = artifact_pair

    summary = validation.validate_dataset_outputs(data_root, None)

    assert summary == validation.ValidationSummary(files=1, rows=1)


def test_validate_dataset_outputs_checks_source_bytes_only_when_a_source_root_is_given(
    artifact_pair: tuple[Path, Path, Path],
) -> None:
    data_root, source_root, _parquet = artifact_pair
    (source_root / "region.osm.pbf").write_bytes(b"changed after finalization")

    assert validation.validate_dataset_outputs(data_root, None) == (
        validation.ValidationSummary(files=1, rows=1)
    )
    with pytest.raises(StorageError, match="source identity mismatch for region.osm.pbf"):
        validation.validate_dataset_outputs(data_root, source_root)
