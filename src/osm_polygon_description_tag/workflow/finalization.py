"""Dataset finalization for the resumable build-and-publish workflow.

The run-level orchestrator delegates three concerns here: regenerating the
deterministic dataset documentation, proving that every discovered source has
a valid local artifact, and publishing/verifying final metadata.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import (
    file_sha256,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.osm.discovery import Source
from osm_polygon_description_tag.publication.models import REPO_ID, PublicationError, UploadPlan
from osm_polygon_description_tag.publication.planning import (
    AREA_HISTOGRAM_ASSET_RELATIVE,
    DATASET_CARD_HERO_ASSET_RELATIVE,
    H3_MAP_ASSET_RELATIVE,
    _build_metadata_only_upload_plan,
    create_upload_plan,
    metadata_only_command,
)
from osm_polygon_description_tag.publication.state import (
    PublicationStateError,
)
from osm_polygon_description_tag.publication.state import (
    _metadata_state_matches as _state_metadata_state_matches,
)
from osm_polygon_description_tag.publication.state import (
    _write_metadata_state as _state_write_metadata_state,
)
from osm_polygon_description_tag.publication.state import (
    cast_dict as _state_cast_dict,
)
from osm_polygon_description_tag.publication.state import (
    read_publication_state as _state_read_publication_state,
)
from osm_polygon_description_tag.publication.upload import execute_upload
from osm_polygon_description_tag.publication.verification import HubVerifier
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.runtime.resources import dataset_card_template
from osm_polygon_description_tag.workflow.source_runner import OrchestratorError


def _translate_state_error(error: PublicationStateError) -> OrchestratorError:
    return OrchestratorError(str(error))


def _read_publication_state(data_root: Path) -> dict[str, object]:
    try:
        return _state_read_publication_state(data_root)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _cast_dict(value: object) -> dict[str, object]:
    try:
        return _state_cast_dict(value)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _metadata_state_matches(data_root: Path, metadata_plan: UploadPlan) -> bool:
    try:
        return _state_metadata_state_matches(data_root, metadata_plan)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _write_metadata_state(
    data_root: Path,
    *,
    identity_sha256: str,
    readme_sha256: str,
    stats_sha256: str,
    readme_size_bytes: int,
    stats_size_bytes: int,
    h3_map_sha256: str | None = None,
    h3_map_size_bytes: int | None = None,
    area_histogram_sha256: str | None = None,
    area_histogram_size_bytes: int | None = None,
    dataset_card_hero_sha256: str | None = None,
    dataset_card_hero_size_bytes: int | None = None,
    verified_revision: str,
    completed_at: str,
) -> dict[str, object]:
    try:
        return _state_write_metadata_state(
            data_root,
            identity_sha256=identity_sha256,
            readme_sha256=readme_sha256,
            stats_sha256=stats_sha256,
            readme_size_bytes=readme_size_bytes,
            stats_size_bytes=stats_size_bytes,
            h3_map_sha256=h3_map_sha256,
            h3_map_size_bytes=h3_map_size_bytes,
            area_histogram_sha256=area_histogram_sha256,
            area_histogram_size_bytes=area_histogram_size_bytes,
            dataset_card_hero_sha256=dataset_card_hero_sha256,
            dataset_card_hero_size_bytes=dataset_card_hero_size_bytes,
            verified_revision=verified_revision,
            completed_at=completed_at,
        )
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def refresh_dataset_docs(
    paths: Paths,
    *,
    clock: Callable[[], str],
    logger: RunLogger,
    docs_generator: Callable[..., object] = generate_dataset_docs,
) -> None:
    """Refresh deterministic README, stats, and map artifacts."""
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        return
    try:
        docs_generator(paths.data_root, dataset_card_template(), clock=clock)
    except Exception as error:
        raise OrchestratorError(f"dataset card refresh failed: {error}") from error
    logger.event(
        "dataset_docs_refreshed",
        level="INFO",
        readme=paths.data_root / "README.md",
        stats=paths.data_root / "stats.json",
        assets=paths.data_root / "assets" / "description_polygon_density.png",
    )


def verify_final_completeness(paths: Paths, sources: Iterable[Source]) -> None:
    """Every discovered source must have a complete, resumable local artifact."""
    discovered = {source.name: source for source in sources}
    completed: set[str] = set()
    extra_artifacts: list[str] = []
    missing_artifacts: list[str] = []
    for parquet in sorted((paths.data_root / "data").glob("*.parquet")):
        stem = parquet.name.removesuffix(".parquet")
        source_name = f"{stem}.osm.pbf"
        if source_name not in discovered:
            extra_artifacts.append(stem)
            continue
        manifest_path = paths.data_root / "manifests" / f"{stem}.manifest.json"
        if not manifest_path.is_file():
            missing_artifacts.append(f"{stem}.manifest.json")
            continue
        try:
            validate_geoparquet(parquet)
            manifest = read_manifest(manifest_path)
        except (StorageError, Exception):
            missing_artifacts.append(f"{stem} (invalid manifest or parquet)")
            continue
        source = discovered[source_name]
        if is_resumable(
            manifest,
            source_identity_for(source.path),
            output_identity_for(parquet),
        ):
            completed.add(source_name)
        else:
            missing_artifacts.append(f"{stem} (not resumable)")
    missing = set(discovered) - completed
    if missing or extra_artifacts or missing_artifacts:
        raise OrchestratorError(
            "final completeness failed: "
            f"missing={sorted(missing)} extra={sorted(extra_artifacts)} "
            f"incomplete={sorted(missing_artifacts)}"
        )


def upload_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
    upload_timeout: float | None,
    clock: Callable[[], str],
    logger: RunLogger | None = None,
    plan_validator: Callable[[Path], object] = create_upload_plan,
) -> str | None:
    """Upload and verify final metadata, updating state only after success."""
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        return None
    metadata_plan = _build_metadata_only_upload_plan(paths.data_root)
    plan_validator(paths.data_root)

    if _metadata_state_matches(paths.data_root, metadata_plan):
        state_payload = _read_publication_state(paths.data_root)
        metadata_state = _cast_dict(state_payload.get("metadata", {}))
        revision = metadata_state.get("verified_revision")
        if logger is not None:
            logger.event(
                "metadata_skip",
                level="INFO",
                verified_revision=str(revision) if revision else "",
            )
        return str(revision) if revision else None

    if logger is not None:
        logger.event("metadata_upload_start", level="INFO")
    try:
        if upload_runner is None:
            execute_upload(
                metadata_plan,
                confirmation=metadata_plan.identity_sha256,
                timeout=upload_timeout,
                retry_observer=(
                    None
                    if logger is None
                    else lambda **fields: logger.event("upload_retry", stage="metadata", **fields)
                ),
            )
        else:
            revision = upload_runner(metadata_only_command(paths.data_root))
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except KeyboardInterrupt:
        raise
    except (PublicationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OrchestratorError(f"final metadata upload failed: {error}") from error
    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; cannot record final revision")
    if logger is not None:
        logger.event("metadata_upload_complete", level="INFO")
        logger.event("metadata_verification_start", level="INFO")
    try:
        verified = verifier(REPO_ID, metadata_plan.files)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise OrchestratorError(f"final Hub verifier failed: {error}") from error
    if not verified:
        raise OrchestratorError("Hub verifier returned no revision for final metadata")
    if logger is not None:
        logger.event(
            "metadata_verification_complete",
            level="INFO",
            verified_revision=verified,
        )

    map_path = paths.data_root / H3_MAP_ASSET_RELATIVE
    histogram_path = paths.data_root / AREA_HISTOGRAM_ASSET_RELATIVE
    hero_path = paths.data_root / DATASET_CARD_HERO_ASSET_RELATIVE
    _write_metadata_state(
        paths.data_root,
        identity_sha256=metadata_plan.identity_sha256,
        readme_sha256=file_sha256(paths.data_root / "README.md"),
        stats_sha256=file_sha256(paths.data_root / "stats.json"),
        readme_size_bytes=(paths.data_root / "README.md").stat().st_size,
        stats_size_bytes=(paths.data_root / "stats.json").stat().st_size,
        h3_map_sha256=file_sha256(map_path),
        h3_map_size_bytes=map_path.stat().st_size,
        area_histogram_sha256=file_sha256(histogram_path),
        area_histogram_size_bytes=histogram_path.stat().st_size,
        dataset_card_hero_sha256=file_sha256(hero_path),
        dataset_card_hero_size_bytes=hero_path.stat().st_size,
        verified_revision=verified,
        completed_at=clock(),
    )
    if logger is not None:
        logger.event(
            "metadata_state_written",
            level="INFO",
            verified_revision=verified,
        )
    return verified


__all__ = ["refresh_dataset_docs", "upload_final_metadata", "verify_final_completeness"]
