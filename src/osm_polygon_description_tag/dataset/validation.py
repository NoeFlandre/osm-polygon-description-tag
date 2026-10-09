"""Verify finalized dataset outputs: each Parquet, its manifest, and optionally its raw source.

A Parquet and its manifest pass only when the Parquet is valid GeoParquet, its
bytes match the manifest, the manifest names the source that produced it, and
the manifest's counts agree. When a source root is given, the raw PBF bytes
must also match the manifest. Every failure raises ``StorageError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    SourceIdentity,
    output_identity_for,
    source_identity_for,
)
from osm_polygon_description_tag.dataset.storage_artifacts import validate_finalized_artifacts
from osm_polygon_description_tag.dataset.storage_errors import StorageError
from osm_polygon_description_tag.dataset.storage_validation import validate_geoparquet


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """How many artifact pairs were verified and how many rows they hold."""

    files: int
    rows: int


def validate_dataset_outputs(data_root: Path, source_root: Path | None) -> ValidationSummary:
    """Verify every finalized artifact under ``data_root``, failing on the first problem."""
    data_dir = data_root / "data"
    if not data_dir.is_dir():
        raise StorageError(f"missing data directory: {data_dir}")
    parquets, manifest_records = _finalized_artifacts(data_root, data_dir)
    rows_total = _validate_artifact_pairs(parquets, manifest_records, source_root)
    return ValidationSummary(files=len(parquets), rows=rows_total)


def _finalized_artifacts(
    data_root: Path, data_dir: Path
) -> tuple[tuple[Path, ...], tuple[Manifest, ...]]:
    artifacts = validate_finalized_artifacts(data_root, require_current_contract=True)
    parquets = artifacts["parquets"]
    if not parquets:
        raise StorageError(f"no finalized data artifacts found in {data_dir}")
    return parquets, artifacts["manifest_records"]


def _validate_artifact_pairs(
    parquets: tuple[Path, ...],
    manifest_records: tuple[Manifest, ...],
    source_root: Path | None,
) -> int:
    return sum(
        _validate_artifact_pair(parquet, manifest, source_root)
        for parquet, manifest in zip(parquets, manifest_records, strict=True)
    )


def _validate_artifact_pair(parquet: Path, manifest: Manifest, source_root: Path | None) -> int:
    rows = validate_geoparquet(parquet, expected_source_pbf=manifest.source.name)
    try:
        output_identity = output_identity_for(parquet)
    except OSError as error:
        raise StorageError(f"cannot read finalized artifact {parquet}: {error}") from error
    if output_identity != manifest.output:
        raise StorageError(f"stale output identity for {parquet.name}")
    _validate_manifest_output_name(parquet, manifest)
    _validate_manifest_row_count(parquet, manifest, rows)
    _validate_manifest_counts(parquet, manifest)
    if source_root is not None:
        validate_source_file_identity(manifest, source_root)
    return rows


def _validate_manifest_output_name(parquet: Path, manifest: Manifest) -> None:
    expected_output_name = f"{manifest.source.name.removesuffix('.osm.pbf')}.parquet"
    if expected_output_name != parquet.name:
        raise StorageError(
            f"manifest source identity mismatch for {parquet.name}: "
            f"source {manifest.source.name!r} maps to {expected_output_name!r}"
        )


def _validate_manifest_row_count(parquet: Path, manifest: Manifest, rows: int) -> None:
    if rows != manifest.counts.included_rows:
        raise StorageError(
            f"manifest row count mismatch for {parquet.name}: "
            f"recorded {manifest.counts.included_rows}, found {rows}"
        )


def _validate_manifest_counts(parquet: Path, manifest: Manifest) -> None:
    expected_emitted = manifest.counts.included_rows + sum(manifest.counts.rejections.values())
    if manifest.counts.emitted_features != expected_emitted:
        raise StorageError(f"manifest counts are inconsistent for {parquet.name}")


def validate_source_file_identity(manifest: Manifest, source_root: Path) -> None:
    """Check that the raw PBF named by ``manifest`` still has the bytes it recorded."""
    source_path = require_regular_source_file(source_root / manifest.source.name)
    source_identity = read_source_identity(source_path)
    if source_identity != manifest.source:
        raise StorageError(f"source identity mismatch for {manifest.source.name}")


def require_regular_source_file(source_path: Path) -> Path:
    """Return ``source_path`` if it is a regular file, not a symlink, directory or missing entry."""
    try:
        is_symlink = source_path.is_symlink()
        is_regular_file = source_path.is_file()
    except OSError as error:
        raise StorageError(f"cannot inspect source file {source_path}: {error}") from error
    if is_symlink or not is_regular_file:
        raise StorageError(f"source identity mismatch: missing regular source file {source_path}")
    return source_path


def read_source_identity(source_path: Path) -> SourceIdentity:
    """Hash the raw source file, reporting read failures with its path."""
    try:
        return source_identity_for(source_path)
    except OSError as error:
        raise StorageError(f"cannot read source file {source_path}: {error}") from error


__all__ = [
    "ValidationSummary",
    "read_source_identity",
    "require_regular_source_file",
    "validate_dataset_outputs",
    "validate_source_file_identity",
]
