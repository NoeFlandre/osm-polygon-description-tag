"""Stoppable, resumable per-PBF build + publish orchestrator.

The single public command (``run_and_publish``) performs a preflight,
iterates discovered sources in deterministic filename order, and for each one:

1. Reuses the existing output if its manifest agrees with the current
   source/output identity, schema versions, area-policy checksum, transform
   algorithm version, and output algorithm revision.
2. Otherwise rebuilds exactly that PBF with the real osmium binary.
3. Validates all local artifacts and globally deduplicates OSM identities with
   an atomic, resumable promotion.
4. Regenerates ``stats.json`` and ``README.md``, then computes per-PBF upload
   plans containing the final Parquet, manifest, README, stats, and visual
   assets.
5. Uploads through :func:`publication.execute_upload` (or the in-process
   test runner), verifies every uploaded file via the default Hub verifier,
   and records each verified remote commit SHA atomically.

Three mutually exclusive per-source outcomes are exposed:

- ``built-needs-upload`` — a fresh parquet was produced and must be uploaded.
- ``reused-local-needs-upload`` — a valid local artifact exists but has no
  matching remote state, so it must be uploaded.
- ``already-published`` — the local artifact matches the publication state, so
  nothing must happen on disk and nothing must be uploaded.

A Ctrl-C interrupt terminates the active osmium child, removes only owned
temporary files, leaves prior artifacts intact, and exits with code 130.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.deduplication import deduplicate_dataset
from osm_polygon_description_tag.dataset.manifest import (
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.observability.trackio import TrackioRecorder
from osm_polygon_description_tag.osm.discovery import Source, discover_sources
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
)
from osm_polygon_description_tag.publication.planning import (
    _build_metadata_only_upload_plan,  # noqa: F401
    _build_per_pbf_upload_plan,
    create_upload_plan,
    per_pbf_command,
)
from osm_polygon_description_tag.publication.state import (
    PUBLICATION_STATE_FILENAME,
    PublicationStateError,
)
from osm_polygon_description_tag.publication.state import (
    _write_publication_state as _state_write_publication_state,
)
from osm_polygon_description_tag.publication.state import (
    cast_dict as _state_cast_dict,
)
from osm_polygon_description_tag.publication.state import (
    read_publication_state as _state_read_publication_state,
)
from osm_polygon_description_tag.publication.upload import execute_upload
from osm_polygon_description_tag.publication.verification import (
    HubVerificationError,
    HubVerifier,
    default_hub_verifier_factory,
)
from osm_polygon_description_tag.runtime.cleanup import cleanup_stale_owned_temps
from osm_polygon_description_tag.runtime.config import Paths
from osm_polygon_description_tag.runtime.logging import RunLogger
from osm_polygon_description_tag.workflow import finalization
from osm_polygon_description_tag.workflow.build import build_one  # noqa: F401
from osm_polygon_description_tag.workflow.preflight import (
    Preflight,
    PreflightError,
    default_preflight,
)
from osm_polygon_description_tag.workflow.source_runner import (
    STATUS_BUILT,
    STATUS_PUBLISHED,
    STATUS_REUSED,
    OrchestratorError,
    SourceOutcome,
)
from osm_polygon_description_tag.workflow.source_runner import (
    local_artifact_is_complete as _local_artifact_is_complete,  # noqa: F401
)
from osm_polygon_description_tag.workflow.source_runner import (
    process_one as _process_one,
)
from osm_polygon_description_tag.workflow.source_runner import (
    published_state_matches as _published_state_matches,
)

INTERRUPT_EXIT_CODE = 130

STATUS_FAILED = "failed"


def _translate_state_error(error: PublicationStateError) -> OrchestratorError:
    return OrchestratorError(str(error))


def read_publication_state(data_root: Path) -> dict[str, object]:
    try:
        return _state_read_publication_state(data_root)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _write_publication_state(*args: Any, **kwargs: Any) -> dict[str, object]:
    try:
        return _state_write_publication_state(*args, **kwargs)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


@dataclass
class OrchestrationReport:
    source_count: int
    preflight: dict[str, object]
    outcomes: list[SourceOutcome] = field(default_factory=list)
    final_remote_revision: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "preflight": self.preflight,
            "source_count": self.source_count,
            "outcomes": [
                {
                    "source_name": outcome.source_name,
                    "status": outcome.status,
                    "included_rows": outcome.included_rows,
                    "output_bytes": outcome.output_bytes,
                    "remote_revision": outcome.remote_revision,
                    "note": outcome.note,
                }
                for outcome in self.outcomes
            ],
            "final_remote_revision": self.final_remote_revision,
        }


# ---------------------------------------------------------------------------
# Publication execution (single canonical path)
# ---------------------------------------------------------------------------


def _default_subprocess_runner(command: list[str], *, timeout: float | None = None) -> None:
    """Default production subprocess boundary.

    Private hook so tests can monkeypatch the real ``subprocess.run`` call.
    The signature forwards ``timeout`` so the orchestrator's
    ``upload_timeout`` reaches ``subprocess.run``.
    """
    subprocess.run(  # noqa: S603 - controlled argument array, no shell
        command,
        check=True,
        shell=False,
        timeout=timeout,
    )


def _execute_publication(
    paths: Paths,
    source: Source,
    *,
    verifier: HubVerifier | None,
    timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
    logger: RunLogger | None = None,
) -> str:
    """Build the per-PBF plan, execute the upload, verify remote, return the SHA.

    The plan is constructed immediately before the upload by the canonical
    per-PBF plan builder; it contains exactly the four files for this PBF.
    Production and tests share the same canonical plan builder.
    """
    plan = _build_per_pbf_upload_plan(paths.data_root, source.name)
    # Revalidate the dataset-wide upload plan immediately before upload to
    # catch in-place mutations between build and upload.
    create_upload_plan(paths.data_root)
    _upload_source_plan(
        plan,
        paths,
        source,
        timeout=timeout,
        upload_runner=upload_runner,
        logger=logger,
    )
    return _verify_source_plan(plan, source, verifier=verifier, logger=logger)


def _upload_source_plan(
    plan: Any,
    paths: Paths,
    source: Source,
    *,
    timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
    logger: RunLogger | None,
) -> None:
    try:
        if upload_runner is None:
            _run_default_source_upload(plan, timeout=timeout, logger=logger)
        else:
            _run_injected_source_upload(paths, source, upload_runner)
    except KeyboardInterrupt:
        raise
    except (PublicationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OrchestratorError(f"upload failed for {source.name}: {error}") from error


def _run_default_source_upload(
    plan: Any,
    *,
    timeout: float | None,
    logger: RunLogger | None,
) -> None:
    execute_upload(
        plan,
        confirmation=plan.identity_sha256,
        timeout=timeout,
        retry_observer=_source_retry_observer(logger),
    )


def _source_retry_observer(logger: RunLogger | None) -> Callable[..., None] | None:
    if logger is None:
        return None
    return lambda **fields: logger.event("upload_retry", **fields)


def _run_injected_source_upload(
    paths: Paths,
    source: Source,
    upload_runner: Callable[[list[str]], str],
) -> None:
    revision = upload_runner(per_pbf_command(paths.data_root, source.name))
    if not revision:
        raise PublicationError("upload runner returned empty revision")


def _verify_source_plan(
    plan: Any,
    source: Source,
    *,
    verifier: HubVerifier | None,
    logger: RunLogger | None,
) -> str:
    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; refusing to record an unknown revision")
    _log_verification_start(logger, source)
    verified = _call_source_verifier(plan, source, verifier)
    _log_verification_complete(logger, source, verified)
    return verified


def _log_verification_start(logger: RunLogger | None, source: Source) -> None:
    if logger is not None:
        logger.event("upload_complete", level="INFO", source=source.name)
        logger.event("verification_start", level="INFO", source=source.name)


def _call_source_verifier(
    plan: Any,
    source: Source,
    verifier: HubVerifier,
) -> str:
    try:
        verified = verifier(REPO_ID, plan.files)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise OrchestratorError(f"Hub verifier failed for {source.name}: {error}") from error
    if not verified:
        raise OrchestratorError(
            f"Hub verifier returned no revision for {source.name}; refusing to record 'unknown'"
        )
    return verified


def _log_verification_complete(
    logger: RunLogger | None,
    source: Source,
    verified: str,
) -> None:
    if logger is not None:
        logger.event(
            "verification_complete",
            level="INFO",
            source=source.name,
            verified_revision=verified,
        )


def _publish_source_if_needed(
    paths: Paths,
    source: Source,
    outcome: SourceOutcome,
    *,
    verifier: HubVerifier | None,
    upload_timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
    clock: Callable[[], str],
    logger: RunLogger,
    source_index: int,
    source_total: int,
) -> tuple[SourceOutcome, bool]:
    """Upload one final deduplicated source artifact when its state is stale."""
    output_path = paths.data_root / "data" / source.output_name
    manifest_name = f"{source.output_name.removesuffix('.parquet')}.manifest.json"
    manifest = read_manifest(paths.data_root / "manifests" / manifest_name)
    outcome.included_rows = manifest.counts.included_rows
    outcome.output_bytes = output_path.stat().st_size
    state = _state_read_publication_state(paths.data_root)
    published = _state_cast_dict(state.get("published", {}))
    existing = _state_cast_dict(published.get(source.name, {}))
    if _published_state_matches(existing, manifest, source, output_path):
        outcome.status = STATUS_PUBLISHED
        outcome.note = "already published; nothing to do"
        return outcome, False

    if outcome.status == STATUS_PUBLISHED:
        outcome.status = STATUS_REUSED
        outcome.note = "deduplicated artifact requires upload"
    logger.event(
        "upload_start",
        level="INFO",
        source=source.name,
        source_index=source_index,
        source_total=source_total,
    )
    try:
        revision = _execute_publication(
            paths,
            source,
            verifier=verifier,
            timeout=upload_timeout,
            upload_runner=upload_runner,
            logger=logger,
        )
    except OrchestratorError as error:
        outcome.status = STATUS_FAILED
        outcome.note = str(error)
        logger.event(
            "upload_failed",
            level="ERROR",
            source=source.name,
            source_index=source_index,
            source_total=source_total,
            reason=str(error),
        )
        raise
    output_identity = output_identity_for(output_path)
    plan_identity = _build_per_pbf_upload_plan(paths.data_root, source.name).identity_sha256
    _write_publication_state(
        paths.data_root,
        source_name=source.name,
        source_sha256=source_identity_for(source.path).sha256,
        output_sha256=output_identity.sha256,
        output_bytes=output_path.stat().st_size,
        remote_revision=revision,
        artifact_identity=plan_identity,
        completed_at=clock(),
    )
    logger.event(
        "state_written",
        level="INFO",
        source=source.name,
        source_index=source_index,
        source_total=source_total,
    )
    outcome.remote_revision = revision
    outcome.note = "published after verified upload"
    return outcome, True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_and_publish(
    *,
    source_root: Path | None = None,
    data_root: Path | None = None,
    confirm_repo: str,
    preflight: Preflight | None = None,
    upload_runner: Callable[[list[str]], str] | None = None,
    clock: Callable[[], str] | None = None,
    paths: Paths | None = None,
    exporter: Callable[..., Iterable[ExportRecord]] | None = None,
    verifier: HubVerifier | None = None,
    verifier_factory: Callable[[], HubVerifier] | None = None,
    upload_timeout: float | None = None,
    subprocess_runner: Callable[[list[str]], None] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    tracker: TrackioRecorder | None = None,
    osmium_executable: str = "osmium",
) -> OrchestrationReport:
    """Stoppable, resumable build + publish for every discovered PBF.

    The default production path uses the real Hugging Face Hub API for
    verification and ``subprocess.run`` for the upload. Tests may inject
    ``upload_runner`` (a callable receiving the canonical command) and/or
    ``verifier`` (a callable returning a SHA on success) without exposing
    any of these via the public CLI.
    """
    clock = _resolve_clock(clock)
    logger, owns_logger = _ensure_logger(logger, paths=paths, data_root=data_root, clock=clock)
    try:
        return _run_with_optional_subprocess_bridge(
            subprocess_runner,
            source_root=source_root,
            data_root=data_root,
            confirm_repo=confirm_repo,
            preflight=preflight,
            upload_runner=upload_runner,
            clock=clock,
            paths=paths,
            exporter=exporter,
            verifier=verifier,
            verifier_factory=verifier_factory,
            upload_timeout=upload_timeout,
            progress_interval=progress_interval,
            logger=logger,
            tracker=tracker,
            osmium_executable=osmium_executable,
        )
    except KeyboardInterrupt:
        logger.event("interrupted", level="WARNING", stage="run-and-publish")
        raise
    finally:
        if owns_logger:
            logger.close()
        if tracker is not None:
            tracker.finish()


def _resolve_clock(clock: Callable[[], str] | None) -> Callable[[], str]:
    return _default_clock if clock is None else clock


def _ensure_logger(
    logger: RunLogger | None,
    *,
    paths: Paths | None,
    data_root: Path | None,
    clock: Callable[[], str],
) -> tuple[RunLogger, bool]:
    if logger is not None:
        return logger, False
    data_root_path = data_root if paths is None else paths.data_root
    if data_root_path is None:
        raise OrchestratorError("logger requires paths or data_root")
    return (
        RunLogger(
            data_root=data_root_path,
            run_id=str(uuid.uuid4()),
            clock=clock,
            buffer_preflight=True,
        ),
        True,
    )


def _run_with_optional_subprocess_bridge(
    subprocess_runner: Callable[[list[str]], None] | None,
    **kwargs: Any,
) -> OrchestrationReport:
    if subprocess_runner is None:
        return _run_and_publish(**kwargs)
    return _run_with_subprocess_bridge(subprocess_runner, **kwargs)


def _run_with_subprocess_bridge(
    subprocess_runner: Callable[[list[str]], None],
    **kwargs: Any,
) -> OrchestrationReport:
    import osm_polygon_description_tag.publication.upload as pub

    original_runner = pub._default_runner_with_retry

    def _bridge(
        command: list[str],
        *,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        backoff_factor: float = 2.0,
        backoff_cap_seconds: float = 60.0,
        timeout: float | None = None,
        _runner: Callable[[list[str], float | None], None] | None = None,
        retry_observer: Callable[..., None] | None = None,
    ) -> None:
        subprocess_runner(command)
        # pragma: no mutate start - compatibility-only retry parameters are intentionally ignored
        _ = (
            max_retries,
            backoff_seconds,
            backoff_factor,
            backoff_cap_seconds,
            timeout,
            _runner,
            retry_observer,
        )
        # pragma: no mutate end

    pub._default_runner_with_retry = _bridge
    try:
        return _run_and_publish(**kwargs)
    finally:
        pub._default_runner_with_retry = original_runner


def _run_and_publish(
    *,
    source_root: Path | None,
    data_root: Path | None,
    confirm_repo: str,
    preflight: Preflight | None,
    upload_runner: Callable[[list[str]], str] | None,
    clock: Callable[[], str],
    paths: Paths | None,
    exporter: Callable[..., Iterable[ExportRecord]] | None,
    verifier: HubVerifier | None,
    verifier_factory: Callable[[], HubVerifier] | None,
    upload_timeout: float | None,
    progress_interval: int,
    logger: RunLogger,
    tracker: TrackioRecorder | None,
    osmium_executable: str,
) -> OrchestrationReport:
    paths = _resolve_paths(paths, source_root, data_root)
    preflight_report = _run_preflight(
        paths,
        preflight=preflight,
        confirm_repo=confirm_repo,
        osmium_executable=osmium_executable,
        logger=logger,
    )
    sources, report = _discover_run(
        paths,
        preflight_report,
        logger=logger,
        tracker=tracker,
    )
    active_verifier = _resolve_verifier(
        paths,
        verifier=verifier,
        verifier_factory=verifier_factory,
        upload_runner=upload_runner,
    )
    outcomes_by_source = _build_sources(
        sources,
        paths,
        clock=clock,
        exporter=exporter,
        progress_interval=progress_interval,
        logger=logger,
        osmium_executable=osmium_executable,
    )
    _finalize_local_dataset(paths, sources, clock=clock, logger=logger)
    _publish_sources(
        paths,
        sources,
        outcomes_by_source,
        report,
        verifier=active_verifier,
        upload_timeout=upload_timeout,
        upload_runner=upload_runner,
        clock=clock,
        logger=logger,
        tracker=tracker,
    )
    _reconcile_remote(paths, active_verifier, logger)
    report.final_remote_revision = _publish_final_metadata(
        paths,
        verifier=active_verifier,
        upload_runner=upload_runner,
        upload_timeout=upload_timeout,
        clock=clock,
        logger=logger,
    )
    logger.event(
        "run_summary",
        level="INFO",
        result="completed",
        source_count=len(sources),
        per_pbf_uploads=sum(outcome.status != STATUS_PUBLISHED for outcome in report.outcomes),
    )
    if tracker is not None:
        tracker.log_snapshot(paths.data_root)
    logger.flush()
    return report


def _resolve_paths(
    paths: Paths | None,
    source_root: Path | None,
    data_root: Path | None,
) -> Paths:
    if paths is not None:
        return paths
    if source_root is None or data_root is None:
        raise OrchestratorError("paths or (source_root, data_root) is required")
    return Paths(source_root=source_root, data_root=data_root)


def _run_preflight(
    paths: Paths,
    *,
    preflight: Preflight | None,
    confirm_repo: str,
    osmium_executable: str,
    logger: RunLogger,
) -> dict[str, object]:
    try:
        report = (
            default_preflight(
                paths,
                confirm_repo=confirm_repo,
                osmium_executable=osmium_executable,
                hf_executable="hf",
            )
            if preflight is None
            else preflight()
        )
    except Exception as error:
        logger.event("preflight_denied", level="ERROR", reason=str(error))
        logger.deny_preflight()
        raise
    logger.event(
        "preflight",
        level="INFO",
        osmium_executable=report.get("osmium_executable", "osmium"),
        osmium_version=report.get("osmium_version", ""),
        hub_repo_sha=report.get("hub_repo_sha", ""),
        source_count=report.get("source_count", 0),
    )
    logger.approve_preflight()
    return report


def _discover_run(
    paths: Paths,
    preflight_report: dict[str, object],
    *,
    logger: RunLogger,
    tracker: TrackioRecorder | None,
) -> tuple[list[Source], OrchestrationReport]:
    removed_temps = cleanup_stale_owned_temps(paths.data_root)
    if removed_temps:
        logger.event("stale_temp_cleanup", level="INFO", rows=len(removed_temps))
    sources = list(discover_sources(paths.source_root))
    report = OrchestrationReport(source_count=len(sources), preflight=preflight_report)
    logger.event("sources_discovered", level="INFO", total=len(sources))
    if tracker is not None:
        tracker.start(
            config={
                "source_count": len(sources),
                "step_definition": "PBF index sorted by filename; not time",
            }
        )
    return sources, report


def _resolve_verifier(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    verifier_factory: Callable[[], HubVerifier] | None,
    upload_runner: Callable[[list[str]], str] | None,
) -> HubVerifier | None:
    if verifier is not None:
        return verifier
    if verifier_factory is not None:
        return verifier_factory()
    if upload_runner is not None:
        return None
    return _default_verifier(paths)


def _default_verifier(paths: Paths) -> HubVerifier:
    try:
        return default_hub_verifier_factory(
            cache_dir=paths.data_root / ".cache" / "huggingface" / "hub"
        )
    except TypeError as error:
        if "unexpected keyword argument" not in str(error):
            raise
        return default_hub_verifier_factory()


def _build_sources(
    sources: list[Source],
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecord]] | None,
    progress_interval: int,
    logger: RunLogger,
    osmium_executable: str,
) -> dict[str, SourceOutcome]:
    outcomes: dict[str, SourceOutcome] = {}
    for index, source in enumerate(sources, start=1):
        outcomes[source.name] = _process_one(
            source,
            paths,
            clock=clock,
            exporter=exporter,
            progress_interval=progress_interval,
            logger=logger,
            source_index=index,
            source_total=len(sources),
            osmium_executable=osmium_executable,
        )
    return outcomes


def _finalize_local_dataset(
    paths: Paths,
    sources: list[Source],
    *,
    clock: Callable[[], str],
    logger: RunLogger,
) -> None:
    _verify_final_completeness(paths, sources)
    dedup_result = deduplicate_dataset(paths.data_root)
    logger.event(
        "deduplication_complete",
        level="INFO",
        input_rows=dedup_result.input_rows,
        output_rows=dedup_result.output_rows,
        duplicate_rows=dedup_result.duplicate_rows,
        files_changed=dedup_result.files_changed,
        status=dedup_result.status,
    )
    _verify_final_completeness(paths, sources)
    _refresh_dataset_docs_for_metadata(paths, clock=clock, logger=logger)


def _publish_sources(
    paths: Paths,
    sources: list[Source],
    outcomes_by_source: dict[str, SourceOutcome],
    report: OrchestrationReport,
    *,
    verifier: HubVerifier | None,
    upload_timeout: float | None,
    upload_runner: Callable[[list[str]], str] | None,
    clock: Callable[[], str],
    logger: RunLogger,
    tracker: TrackioRecorder | None,
) -> None:
    cumulative_rows = 0
    cumulative_output_bytes = 0
    for index, source in enumerate(sources, start=1):
        outcome, _uploaded = _publish_source_if_needed(
            paths,
            source,
            outcomes_by_source[source.name],
            verifier=verifier,
            upload_timeout=upload_timeout,
            upload_runner=upload_runner,
            clock=clock,
            logger=logger,
            source_index=index,
            source_total=len(sources),
        )
        report.outcomes.append(outcome)
        cumulative_rows += outcome.included_rows
        cumulative_output_bytes += outcome.output_bytes
        if tracker is not None:
            tracker.log(
                {
                    "step": index,
                    "cumulative_rows": cumulative_rows,
                    "cumulative_output_bytes": cumulative_output_bytes,
                }
            )


def _reconcile_remote(
    paths: Paths,
    verifier: HubVerifier | None,
    logger: RunLogger,
) -> None:
    reconcile = getattr(verifier, "reconcile_managed_files", None)
    if not callable(reconcile):
        return
    full_plan = create_upload_plan(paths.data_root)
    logger.event("remote_reconciliation_start", level="INFO")
    revision = _call_remote_reconcile(reconcile, full_plan)
    logger.event(
        "remote_reconciliation_complete",
        level="INFO",
        verified_revision=str(revision or ""),
    )


def _call_remote_reconcile(reconcile: Callable[..., object], plan: Any) -> object:
    try:
        return reconcile(REPO_ID, {item.relative_path for item in plan.files})
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise OrchestratorError(f"remote artifact reconciliation failed: {error}") from error


def _publish_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
    upload_timeout: float | None,
    clock: Callable[[], str],
    logger: RunLogger,
) -> str | None:
    return _upload_final_metadata(
        paths,
        verifier=verifier,
        upload_runner=upload_runner,
        upload_timeout=upload_timeout,
        clock=clock,
        logger=logger,
    )


def _refresh_dataset_docs_for_metadata(
    paths: Paths,
    *,
    clock: Callable[[], str],
    logger: RunLogger,
) -> None:
    """Compatibility wrapper for the canonical finalization module."""
    finalization.refresh_dataset_docs(
        paths,
        clock=clock,
        logger=logger,
        docs_generator=generate_dataset_docs,
    )


def _verify_final_completeness(paths: Paths, sources: Iterable[Source]) -> None:
    """Compatibility wrapper for final artifact validation."""
    finalization.verify_final_completeness(paths, sources)


def _upload_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
    upload_timeout: float | None = None,
    clock: Callable[[], str] | None = None,
    logger: RunLogger | None = None,
) -> str | None:
    """Compatibility wrapper for final metadata publication."""
    if clock is None:
        clock = _default_clock
    return finalization.upload_final_metadata(
        paths,
        verifier=verifier,
        upload_runner=upload_runner,
        upload_timeout=upload_timeout,
        clock=clock,
        logger=logger,
        plan_validator=create_upload_plan,
    )


def _default_clock() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "INTERRUPT_EXIT_CODE",
    "PUBLICATION_STATE_FILENAME",
    "STATUS_BUILT",
    "STATUS_FAILED",
    "STATUS_PUBLISHED",
    "STATUS_REUSED",
    "HubVerificationError",
    "OrchestrationReport",
    "OrchestratorError",
    "PreflightError",
    "SourceOutcome",
    "create_upload_plan",
    "default_hub_verifier_factory",
    "default_preflight",
    "read_publication_state",
    "run_and_publish",
]
