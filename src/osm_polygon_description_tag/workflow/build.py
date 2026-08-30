"""Sequential, resumable composition of the polygon description build.

``build_one`` validates boundaries, streams versioned exports, transforms and
counts features, atomically writes and validates GeoParquet, and records a
manifest. ``build_all`` is deliberately sequential and fail-fast because osmium
area assembly can be memory-intensive.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    TRANSFORM_ALGORITHM_VERSION,
    Manifest,
    ManifestError,
    RunCounts,
    current_area_policy_sha256,
    current_code_revision,
    current_dependency_versions,
    current_output_algorithm_revision,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
    write_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA_VERSION
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    validate_geoparquet,
    write_geoparquet,
)
from osm_polygon_description_tag.dataset.transform import (
    RejectedFeature,
    _early_rejection_reason,
    transform_record,
)
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.osm.extraction import (
    ExportRecord,
    OsmiumExportError,
    osmium_version,
    stream_export,
)
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.time import utc_now_iso

_GEOPARQUET_VERSION = "1.1.0"


class PipelineError(RuntimeError):
    """Raised for infrastructure failures during a build."""


Exporter = Callable[..., Iterable[ExportRecord]]
Writer = Callable[..., int]
Clock = Callable[[], str]


@dataclass(frozen=True)
class BuildResult:
    source_name: str
    output_name: str
    status: str
    emitted_features: int
    included_rows: int
    rejections: dict[str, int]
    output_path: Path
    manifest_path: Path


@dataclass
class _Counts:
    emitted: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


def safe_osmium_version(executable: str) -> str | None:
    try:
        return osmium_version(executable)
    except OsmiumExportError:
        return None


def _verify_direct_child(source: Source, source_root: Path) -> None:
    if source.path.is_symlink() or not source.path.is_file():
        raise PipelineError(f"source is not a regular direct child: {source.path}")
    if source.path.parent.resolve(strict=False) != source_root.resolve(strict=False):
        raise PipelineError(f"source is not inside source root: {source.path}")


def _transform_stream(
    records: Iterable[ExportRecord],
    source_name: str,
    counts: _Counts,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    progress_interval: int = 100_000,  # pragma: no mutate - public default contract
) -> Iterator[dict[str, object]]:
    included = 0
    last_emitted = 0
    interval = max(int(progress_interval), 1)
    for record in records:
        counts.emitted += 1
        transformed = _transform_one(record, source_name, counts)
        if transformed is not None:
            included += 1
            yield transformed
        if progress_callback is not None and counts.emitted - last_emitted >= interval:
            last_emitted = counts.emitted
            progress_callback(counts.emitted, included)


def _transform_one(
    record: ExportRecord,
    source_name: str,
    counts: _Counts,
) -> dict[str, object] | None:
    early_reason = _early_rejection_reason(record)
    if early_reason is not None:
        counts.rejections[early_reason] = counts.rejections.get(early_reason, 0) + 1
        return None
    try:
        return transform_record(record, source_name)
    except RejectedFeature as rejection:
        counts.rejections[rejection.reason] = counts.rejections.get(rejection.reason, 0) + 1
        return None


def _artifact_paths(source: Source, paths: Paths) -> tuple[Path, Path]:
    data_dir = paths.data_root / "data"
    manifests_dir = paths.data_root / "manifests"
    output_path = data_dir / source.output_name
    manifest_path = manifests_dir / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    for artifact in (output_path, manifest_path):
        if artifact.resolve(strict=False).is_relative_to(paths.source_root.resolve(strict=False)):
            raise PipelineError(f"artifact path inside immutable source: {artifact}")
    data_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(exist_ok=True)
    return output_path, manifest_path


def _reusable_build_result(
    source: Source,
    output_path: Path,
    manifest_path: Path,
) -> BuildResult | None:
    manifest = _resumable_manifest(source, output_path, manifest_path)
    if manifest is None or not _valid_output(output_path):
        return None

    return BuildResult(
        source_name=source.name,
        output_name=source.output_name,
        status="skipped",
        emitted_features=manifest.counts.emitted_features,
        included_rows=manifest.counts.included_rows,
        rejections=dict(manifest.counts.rejections),
        output_path=output_path,
        manifest_path=manifest_path,
    )


def _resumable_manifest(
    source: Source,
    output_path: Path,
    manifest_path: Path,
) -> Manifest | None:
    if not output_path.is_file() or not manifest_path.is_file():
        return None
    try:
        manifest = read_manifest(manifest_path)
    except ManifestError:
        return None
    if not is_resumable(
        manifest,
        source_identity_for(source.path),
        output_identity_for(output_path),
    ):
        return None
    return manifest


def _valid_output(output_path: Path) -> bool:
    try:
        validate_geoparquet(output_path)
    except StorageError:
        return False
    return True


def _build_fresh(
    source: Source,
    output_path: Path,
    manifest_path: Path,
    *,
    exporter: Exporter,
    writer: Writer,
    executable: str,
    export_config: Path,
    clock: Clock,
    batch_size: int,
    progress_interval: int,
    progress_callback: Callable[[int, int], None] | None,
) -> BuildResult:
    started_at = clock()
    counts = _Counts()
    transformed = _transform_stream(
        exporter(source.path, export_config),
        source.name,
        counts,
        progress_callback=progress_callback,
        progress_interval=progress_interval,
    )
    included_rows = writer(transformed, output_path, batch_size=batch_size)
    completed_at = clock()
    manifest = Manifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        schema_version=SCHEMA_VERSION,
        geoparquet_version=_GEOPARQUET_VERSION,
        transform_algorithm_version=TRANSFORM_ALGORITHM_VERSION,
        area_policy_sha256=current_area_policy_sha256(),
        output_algorithm_revision=current_output_algorithm_revision(),
        source=source_identity_for(source.path),
        output=output_identity_for(output_path),
        osmium_version=safe_osmium_version(executable),
        dependency_versions=current_dependency_versions(),
        code_revision=current_code_revision(),
        started_at=started_at,
        completed_at=completed_at,
        counts=RunCounts(counts.emitted, included_rows, dict(counts.rejections)),
    )
    write_manifest(manifest, manifest_path)
    return BuildResult(
        source_name=source.name,
        output_name=source.output_name,
        status="built",
        emitted_features=counts.emitted,
        included_rows=included_rows,
        rejections=dict(counts.rejections),
        output_path=output_path,
        manifest_path=manifest_path,
    )


def build_one(
    source: Source,
    paths: Paths,
    *,
    export_config: Path,
    executable: str = "osmium",
    exporter: Exporter | None = None,
    writer: Writer | None = None,
    clock: Clock | None = None,
    batch_size: int = 1024,
    progress_interval: int = 100_000,
    progress_callback: Callable[[int, int], None] | None = None,
) -> BuildResult:
    """Build (or resume) one source's GeoParquet output and manifest."""

    def default_exporter(src: Path, cfg: Path) -> Iterable[ExportRecord]:
        return stream_export(src, cfg, executable=executable)

    exporter = default_exporter if exporter is None else exporter
    writer = write_geoparquet if writer is None else writer
    clock = utc_now_iso if clock is None else clock

    paths.validate()
    _verify_direct_child(source, paths.source_root)
    output_path, manifest_path = _artifact_paths(source, paths)
    reused = _reusable_build_result(source, output_path, manifest_path)
    if reused is not None:
        return reused
    return _build_fresh(
        source,
        output_path,
        manifest_path,
        exporter=exporter,
        writer=writer,
        executable=executable,
        export_config=export_config,
        clock=clock,
        batch_size=batch_size,
        progress_interval=progress_interval,
        progress_callback=progress_callback,
    )


def build_all(
    sources: Iterable[Source],
    *,
    build: Callable[[Source], BuildResult],
) -> list[BuildResult]:
    """Run ``build`` for every source in deterministic order, stopping on first failure."""
    ordered = sorted(sources, key=lambda source: source.name)
    results: list[BuildResult] = []
    for source in ordered:
        results.append(build(source))
    return results
