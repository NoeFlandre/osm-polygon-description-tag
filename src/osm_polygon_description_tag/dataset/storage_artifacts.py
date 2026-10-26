"""Validation of finalized dataset artifacts against their manifests."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestError,
    OutputIdentity,
    is_resumable,
    manifest_path_for,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.storage_errors import StorageError
from osm_polygon_description_tag.dataset.storage_validation import validate_geoparquet


class FinalizedArtifacts(TypedDict):
    """Result of :func:`validate_finalized_artifacts`."""

    parquets: tuple[Path, ...]
    manifests: tuple[Path, ...]
    manifest_records: tuple[Manifest, ...]


@dataclass(frozen=True)
class _ValidatedManifestPair:
    path: Path
    manifest: Manifest


def validate_finalized_artifacts(
    data_root: Path, *, require_current_contract: bool = False
) -> FinalizedArtifacts:
    """Validate every finalized Parquet/manifest pair under data_root.

    The validation is intentionally minimal: it only checks that every
    Parquet has a matching, parseable, schema-current manifest whose
    output identity matches the Parquet and whose ``included_rows`` equals
    the Parquet's row count. It does NOT call
    :func:`validate_geoparquet`; that stricter byte-level validation is
    performed separately, downstream, when the artifact is loaded. Callers
    that report whether an artifact set matches the current dataset contract
    can request that check explicitly. The result includes each parsed
    manifest record so follow-up checks can use the same validated read.
    """
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_artifact_root(data_root)
    _require_artifact_directories(data_dir, manifests_dir)

    parquets = sorted(
        data_dir.glob("*.parquet"), key=lambda p: p.name
    )  # pragma: no mutate - all paths share one parent
    manifest_paths = sorted(
        manifests_dir.glob("*.manifest.json"), key=lambda p: p.name
    )  # pragma: no mutate - all paths share one parent
    _check_artifact_stems(parquets, manifest_paths)

    validated_pairs = [
        _validate_manifest_pair_record(
            parquet, manifests_dir, require_current_contract=require_current_contract
        )
        for parquet in parquets
    ]

    return {
        "parquets": tuple(parquets),
        "manifests": tuple(pair.path for pair in validated_pairs),
        "manifest_records": tuple(pair.manifest for pair in validated_pairs),
    }


def _require_artifact_root(data_root: Path) -> None:
    if data_root.is_symlink():
        raise StorageError(f"data root is not a regular directory: {data_root}")


def _require_artifact_directories(data_dir: Path, manifests_dir: Path) -> None:
    if data_dir.is_symlink():
        raise StorageError(f"data directory must be a real directory: {data_dir}")
    if not data_dir.is_dir():
        raise StorageError(f"missing data directory: {data_dir}")
    if manifests_dir.is_symlink():
        raise StorageError(f"manifest directory must be a real directory: {manifests_dir}")
    if not manifests_dir.is_dir():
        raise StorageError(f"missing manifests directory: {manifests_dir}")


def _check_artifact_stems(parquets: list[Path], manifests: list[Path]) -> None:
    parquet_stems = {p.name.removesuffix(".parquet") for p in parquets}
    manifest_stems = {p.name.removesuffix(".manifest.json") for p in manifests}
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise StorageError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")


def _validate_manifest_pair(
    parquet: Path, manifests_dir: Path, *, require_current_contract: bool = False
) -> Path:
    return _validate_manifest_pair_record(
        parquet, manifests_dir, require_current_contract=require_current_contract
    ).path


def _validate_manifest_pair_record(
    parquet: Path, manifests_dir: Path, *, require_current_contract: bool = False
) -> _ValidatedManifestPair:
    _require_regular_file(parquet, "finalized artifact", "finalized artifact is not a regular file")
    manifest_path = manifest_path_for(parquet.name, manifests_dir.parent)
    _require_regular_file(manifest_path, "manifest", "manifest is not a regular file")
    manifest = _read_paired_manifest(manifest_path)
    _validate_supported_manifest_version(manifest)
    output_identity = _read_paired_output_identity(parquet)
    _validate_paired_output(parquet, manifest, output_identity)
    _validate_current_manifest_contract(
        manifest, output_identity, manifest_path, require_current_contract
    )
    _validate_manifest_included_rows(parquet, manifest)
    return _ValidatedManifestPair(manifest_path, manifest)


def _require_regular_file(path: Path, label: str, invalid_message: str) -> None:
    try:
        is_symlink = path.is_symlink()
        is_regular_file = path.is_file()
    except OSError as error:
        raise StorageError(f"cannot inspect {label} {path}: {error}") from error
    if is_symlink or not is_regular_file:
        raise StorageError(f"{invalid_message}: {path}")


def _read_paired_manifest(manifest_path: Path) -> Manifest:
    try:
        return read_manifest(manifest_path)
    except ManifestError as error:
        raise StorageError(f"invalid manifest {manifest_path}: {error}") from error


def _read_paired_output_identity(parquet: Path) -> OutputIdentity:
    try:
        return output_identity_for(parquet)
    except OSError as error:
        raise StorageError(f"cannot read finalized artifact {parquet}: {error}") from error


def _validate_paired_output(
    parquet: Path, manifest: Manifest, output_identity: OutputIdentity
) -> None:
    if manifest.output != output_identity:
        raise StorageError(f"stale output identity for {parquet.name}")


def _validate_manifest_included_rows(parquet: Path, manifest: Manifest) -> None:
    """Reject a manifest whose recorded ``included_rows`` disagree with the Parquet.

    Every writer records the rows it kept in the Parquet it finalized, so a
    finalized pair always agrees. A disagreement means the counts describe a
    different file than the one on disk, such as a repair interrupted after the
    Parquet was promoted and before its manifest was rewritten.
    """
    rows = _read_parquet_row_count(parquet)
    if rows != manifest.counts.included_rows:
        raise StorageError(
            f"manifest row count mismatch for {parquet.name}: "
            f"recorded {manifest.counts.included_rows}, found {rows}"
        )


def _read_parquet_row_count(parquet: Path) -> int:
    try:
        with contextlib.closing(pq.ParquetFile(parquet)) as reader:
            return reader.metadata.num_rows
    except (OSError, pa.ArrowException) as error:
        raise StorageError(f"cannot read finalized artifact {parquet}: {error}") from error


def _validate_supported_manifest_version(manifest: Manifest) -> None:
    if manifest.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        raise StorageError(
            f"manifest uses unsupported schema version: {manifest.manifest_schema_version}"
        )


def _validate_current_manifest_contract(
    manifest: Manifest,
    output_identity: OutputIdentity,
    manifest_path: Path,
    require_current_contract: bool,
) -> None:
    if require_current_contract and not is_resumable(manifest, manifest.source, output_identity):
        raise StorageError(
            f"manifest contract does not match current configuration: {manifest_path}"
        )


def validate_finalized_artifacts_strict(data_root: Path) -> FinalizedArtifacts:
    """Run :func:`validate_finalized_artifacts` followed by :func:`validate_geoparquet`.

    Use this when downstream code is about to load the validated
    Parquets and so the additional byte-level guarantees of
    :func:`validate_geoparquet` are required.
    """
    result = validate_finalized_artifacts(data_root)
    for parquet in result["parquets"]:
        validate_geoparquet(parquet)
    return result


__all__ = ["validate_finalized_artifacts", "validate_finalized_artifacts_strict"]
