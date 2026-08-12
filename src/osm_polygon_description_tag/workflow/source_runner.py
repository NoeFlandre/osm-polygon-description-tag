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
    state = read_publication_state(paths.data_root)
    published = cast_dict(state.get("published", {}))
    existing = cast_dict(published.get(source.name, {}))
    output_path = paths.data_root / "data" / source.output_name

    if (
        complete
        and manifest is not None
        and published_state_matches(existing, manifest, source, output_path)
    ):
        if logger is not None:
            logger.event(
                "source_decision",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                decision="already-published",
            )
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_PUBLISHED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="already published; nothing to do",
        )

    if complete and manifest is not None:
        if logger is not None:
            logger.event(
                "source_decision",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                decision="reuse-local",
            )
        return SourceOutcome(
            source_name=source.name,
            status=STATUS_REUSED,
            included_rows=manifest.counts.included_rows,
            output_bytes=output_path.stat().st_size,
            remote_revision=None,
            note="local artifact reused; upload required",
        )

    if logger is not None:
        logger.event(
            "source_decision",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            decision="build",
        )
        logger.event(
            "build_start",
            level="INFO",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
        )

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

        progress_callback = on_progress
    else:
        progress_callback = None
    result = build_one(
        source,
        paths,
        export_config=osmium_export_config(),
        executable=osmium_executable,
        exporter=exporter,
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
        remote_revision=None,
        note="freshly built; upload required",
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
