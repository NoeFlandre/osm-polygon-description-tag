"""Per-source state decisions for the resumable workflow.

This module owns the local artifact state machine used by the run-level
orchestrator.  It deliberately has no publication or global reconciliation
responsibilities: given one discovered PBF, it decides whether to build,
reuse, or report an already-published artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.publication.state import (
    PublicationStateError,
)
from osm_polygon_description_tag.publication.state import (
    cast_dict as _state_cast_dict,
)
from osm_polygon_description_tag.publication.state import (
    read_publication_state as _state_read_publication_state,
)
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.resources import osmium_export_config
from osm_polygon_description_tag.workflow.build import build_one

STATUS_BUILT = "built-needs-upload"
STATUS_REUSED = "reused-local-needs-upload"
STATUS_PUBLISHED = "already-published"


class OrchestratorError(RuntimeError):
    """Raised for workflow failures after preflight succeeds."""


OrchestratorError.__module__ = "osm_polygon_description_tag.orchestrator"


def _translate_state_error(error: PublicationStateError) -> OrchestratorError:
    return OrchestratorError(str(error))


def read_publication_state(data_root: Path) -> dict[str, object]:
    try:
        return _state_read_publication_state(data_root)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def cast_dict(value: object) -> dict[str, object]:
    try:
        return _state_cast_dict(value)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


@dataclass
class SourceOutcome:
    """Result of preparing one source for publication."""

    source_name: str
    status: str
    included_rows: int = 0
    output_bytes: int = 0
    remote_revision: str | None = None
    note: str | None = None


def local_artifact_is_complete(paths: Paths, source: Source) -> tuple[bool, Manifest | None]:
    """Return ``(True, manifest)`` only for a fully resumable local artifact."""
    output_path = paths.data_root / "data" / source.output_name
    manifest_path = (
        paths.data_root
        / "manifests"
        / f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    )
    if not output_path.is_file() or not manifest_path.is_file():
        return False, None
    try:
        validate_geoparquet(output_path)
        manifest = read_manifest(manifest_path)
    except (StorageError, Exception):
        return False, None
    if not is_resumable(
        manifest,
        source_identity_for(source.path),
        output_identity_for(output_path),
    ):
        return False, None
    return True, manifest


def published_state_matches(
    existing: dict[str, object],
    manifest: Manifest,
    source: Source,
    output_path: Path,
) -> bool:
    """Return whether publication state matches the current source artifact."""
    source_identity = source_identity_for(source.path)
    output_identity = output_identity_for(output_path)
    if existing.get("source_sha256") != source_identity.sha256:
        return False
    if existing.get("output_sha256") != output_identity.sha256:
        return False
    if manifest.source != source_identity:
        return False
    return manifest.output == output_identity


def _published_entry(data_root: Path, source_name: str) -> dict[str, object]:
    state = read_publication_state(data_root)
    published = cast_dict(state.get("published", {}))
    return cast_dict(published.get(source_name, {}))


def _log_decision(
    logger: RunLogger | None,
    source: Source,
    source_index: int,
    source_total: int,
    decision: str,
) -> None:
    if logger is not None:
        logger.event(
            "source_decision",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            decision=decision,
        )


def _manifest_outcome(
    source: Source,
    manifest: Manifest,
    output_path: Path,
    status: str,
    note: str,
) -> SourceOutcome:
    return SourceOutcome(
        source_name=source.name,
        status=status,
        included_rows=manifest.counts.included_rows,
        output_bytes=output_path.stat().st_size,
        note=note,
    )


def _progress_logger(
    logger: RunLogger,
    source: Source,
    source_index: int,
    source_total: int,
) -> Callable[[int, int], None]:
    def on_progress(emitted: int, included: int) -> None:
        logger.event(
            "build_progress",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            emitted=emitted,
            included=included,
        )

    return on_progress


def _build_source(
    source: Source,
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecord]] | None,
    progress_interval: int,
    logger: RunLogger | None,
    source_index: int,
    source_total: int,
    osmium_executable: str,
) -> SourceOutcome:
    if logger is not None:
        _log_decision(logger, source, source_index, source_total, "build")
        logger.event(
            "build_start",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
        )
        progress_callback = _progress_logger(logger, source, source_index, source_total)
    else:
        progress_callback = None
    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable=osmium_executable,
        exporter=exporter,
        clock=clock,
        progress_interval=progress_interval,
        progress_callback=progress_callback,
    )
    if logger is not None:
        logger.event(
            "build_complete",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            rows=result.included_rows,
            bytes=result.output_path.stat().st_size,
        )
    return SourceOutcome(
        source_name=source.name,
        status=STATUS_BUILT,
        included_rows=result.included_rows,
        output_bytes=result.output_path.stat().st_size,
        note="freshly built; upload required",
    )


def process_one(
    source: Source,
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecord]] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    source_index: int = 0,
    source_total: int = 0,
    osmium_executable: str = "osmium",
) -> SourceOutcome:
    """Build or reuse one PBF and return its local state-machine outcome."""
    complete, manifest = local_artifact_is_complete(paths, source)
    existing = _published_entry(paths.data_root, source.name)
    output_path = paths.data_root / "data" / source.output_name

    if complete and manifest is not None:
        if published_state_matches(existing, manifest, source, output_path):
            _log_decision(logger, source, source_index, source_total, "already-published")
            return _manifest_outcome(
                source,
                manifest,
                output_path,
                STATUS_PUBLISHED,
                "already published; nothing to do",
            )
        _log_decision(logger, source, source_index, source_total, "reuse-local")
        return _manifest_outcome(
            source,
            manifest,
            output_path,
            STATUS_REUSED,
            "local artifact reused; upload required",
        )
    return _build_source(
        source,
        paths,
        clock=clock,
        exporter=exporter,
        progress_interval=progress_interval,
        logger=logger,
        source_index=source_index,
        source_total=source_total,
        osmium_executable=osmium_executable,
    )


__all__ = [
    "STATUS_BUILT",
    "STATUS_PUBLISHED",
    "STATUS_REUSED",
    "OrchestratorError",
    "SourceOutcome",
    "cast_dict",
    "local_artifact_is_complete",
    "process_one",
    "published_state_matches",
    "read_publication_state",
]
