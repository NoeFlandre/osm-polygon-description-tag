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
from osm_polygon_description_tag.dataset.transform import RejectedFeature, transform_record
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
    progress_interval: int = 100_000,
) -> Iterator[dict[str, object]]:
    included = 0
    last_emitted = 0
    interval = max(int(progress_interval), 1)
    for record in records:
        counts.emitted += 1
        try:
            yield transform_record(record, source_name)
        except RejectedFeature as rejection:
            counts.rejections[rejection.reason] = counts.rejections.get(rejection.reason, 0) + 1
        else:
            included += 1
        if progress_callback is not None and counts.emitted - last_emitted >= interval:
            last_emitted = counts.emitted
            progress_callback(counts.emitted, included)


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

    if exporter is None:
        exporter = default_exporter
    if writer is None:
        writer = write_geoparquet
    if clock is None:
        clock = utc_now_iso

    paths.validate()
    _verify_direct_child(source, paths.source_root)

    data_dir = paths.data_root / "data"
    manifests_dir = paths.data_root / "manifests"
    output_path = data_dir / source.output_name
    manifest_path = manifests_dir / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    for artifact in (output_path, manifest_path):
        if artifact.resolve(strict=False).is_relative_to(paths.source_root.resolve(strict=False)):
            raise PipelineError(f"artifact path inside immutable source: {artifact}")

    data_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    if output_path.is_file() and manifest_path.is_file():
        try:
            manifest = read_manifest(manifest_path)
        except ManifestError:
            manifest = None
        if manifest is not None and is_resumable(
            manifest,
            source_identity_for(source.path),
            output_identity_for(output_path),
        ):
            try:
                validate_geoparquet(output_path)
            except StorageError:
                pass
            else:
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
