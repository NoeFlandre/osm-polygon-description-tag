"""Artifact validation and manifest summaries for dataset statistics.

Statistics are derived only from finalized Parquet files whose manifests still
match their bytes. This module finds those artifacts, raises ReportingError when
an artifact or manifest is missing, stale, or inconsistent, and summarizes what
the manifests record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    ManifestError,
    file_sha256,
    manifest_path_for,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.storage_validation import validate_geoparquet


class ReportingError(ValueError):
    """Raised when artifacts/manifests are missing, stale, or inconsistent."""


@dataclass(frozen=True)
class ValidatedArtifact:
    parquet: Path
    manifest: Manifest


@dataclass(frozen=True)
class ManifestSummary:
    emitted_features: int
    rejections: dict[str, int]
    source_bytes_total: int
    output_bytes_total: int
    files: list[dict[str, Any]]


def _reporting_directories(data_root: Path) -> tuple[Path, Path]:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise ReportingError(f"missing data/ or manifests/ under {data_root}")
    return data_dir, manifests_dir


def _matching_parquets(data_dir: Path, manifests_dir: Path) -> list[Path]:
    # pragma: no mutate start - all glob results share data_dir, so Path order equals name order
    parquets = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    # pragma: no mutate end
    parquet_stems = {path.name.removesuffix(".parquet") for path in parquets}
    manifest_stems = {
        path.name.removesuffix(".manifest.json") for path in manifests_dir.glob("*.manifest.json")
    }
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise ReportingError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")
    return parquets


def _validate_artifact(parquet: Path, manifests_dir: Path) -> ValidatedArtifact:
    stem = parquet.name.removesuffix(".parquet")
    try:
        manifest = read_manifest(manifest_path_for(parquet.name, manifests_dir.parent))
    except ManifestError as error:
        raise ReportingError(f"cannot read manifest for {stem}: {error}") from error
    actual_output = output_identity_for(parquet)
    if manifest.output != actual_output:
        raise ReportingError(f"stale output identity for {parquet.name}")
    return ValidatedArtifact(parquet=parquet, manifest=manifest)


def find_validated_artifacts(data_root: Path) -> tuple[ValidatedArtifact, ...]:
    data_dir, manifests_dir = _reporting_directories(data_root)
    parquets = _matching_parquets(data_dir, manifests_dir)
    artifacts = tuple(_validate_artifact(parquet, manifests_dir) for parquet in parquets)
    for artifact in artifacts:
        validate_geoparquet(
            artifact.parquet,
            require_successful_text=False,
        )
    return artifacts


def _rows_in_parquet(path: Path) -> int:
    metadata = pq.ParquetFile(path).metadata
    return int(metadata.num_rows if metadata else 0)


def collect_manifest_summary(
    artifacts: tuple[ValidatedArtifact, ...],
) -> ManifestSummary:
    rejections: dict[str, int] = {}
    emitted_features = 0
    source_bytes = 0
    output_bytes = 0
    file_records: list[dict[str, Any]] = []
    for artifact in artifacts:
        parquet = artifact.parquet
        manifest = artifact.manifest
        output_size = parquet.stat().st_size
        output_bytes += output_size
        source_bytes += manifest.source.size_bytes
        emitted_features += manifest.counts.emitted_features
        for reason, count in manifest.counts.rejections.items():
            rejections[reason] = rejections.get(reason, 0) + count
        file_records.append(
            {
                "source_pbf": manifest.source.name,
                "parquet": parquet.name,
                "rows": _rows_in_parquet(parquet),
                "source_bytes": manifest.source.size_bytes,
                "output_bytes": output_size,
                "emitted_features": manifest.counts.emitted_features,
                "rejections": dict(sorted(manifest.counts.rejections.items())),
                "source_sha256": manifest.source.sha256,
                "output_sha256": file_sha256(parquet),
            }
        )
    file_records.sort(key=lambda record: record["parquet"])
    return ManifestSummary(
        emitted_features=emitted_features,
        rejections=dict(sorted(rejections.items())),
        source_bytes_total=source_bytes,
        output_bytes_total=output_bytes,
        files=file_records,
    )
