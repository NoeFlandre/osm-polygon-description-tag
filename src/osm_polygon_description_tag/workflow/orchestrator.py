"""Stoppable, resumable per-PBF build + publish orchestrator.

The single public command (``run_and_publish``) performs a preflight,
iterates discovered sources in deterministic filename order, and for each one:

1. Reuses the existing output if its manifest agrees with the current
   source/output identity, schema versions, area-policy checksum, transform
   algorithm version, and output algorithm revision.
2. Otherwise rebuilds exactly that PBF with the real osmium binary.
3. Validates the produced GeoParquet and manifest, regenerates
   ``stats.json`` and ``README.md``, and computes a per-PBF upload plan
   containing exactly four files (Parquet, manifest, README, stats).
4. Uploads through :func:`publication.execute_upload` (or the in-process
   test runner), with optional ``upload_timeout`` forwarded to
   ``subprocess.run``.
5. Verifies every uploaded file exists on the Hub with matching identity via
   the default Hub verifier (built on ``huggingface_hub.HfApi``) and
   records the verified remote commit SHA in ``publication-state.json``
   atomically.

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

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    is_resumable,
    output_identity_for,
    read_manifest,
    source_identity_for,
)
from osm_polygon_description_tag.dataset.reporting import generate_dataset_docs
from osm_polygon_description_tag.dataset.storage import StorageError, validate_geoparquet
from osm_polygon_description_tag.osm.discovery import Source, discover_sources
from osm_polygon_description_tag.osm.extraction import ExportRecord
from osm_polygon_description_tag.publication.models import (
    REPO_ID,
    PublicationError,
)
from osm_polygon_description_tag.publication.planning import (
    _build_metadata_only_upload_plan,
    _build_per_pbf_upload_plan,
    create_upload_plan,
    per_pbf_command,
)
from osm_polygon_description_tag.publication.state import (
    PUBLICATION_STATE_FILENAME,
    PublicationStateError,
)
from osm_polygon_description_tag.publication.state import (
    _metadata_state_matches as _state_metadata_state_matches,
)
from osm_polygon_description_tag.publication.state import (
    _write_metadata_state as _state_write_metadata_state,
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
from osm_polygon_description_tag.runtime.resources import (
    dataset_card_template,
    osmium_export_config,
)
from osm_polygon_description_tag.workflow.build import build_one
from osm_polygon_description_tag.workflow.preflight import (
    Preflight,
    PreflightError,
    default_preflight,
)

INTERRUPT_EXIT_CODE = 130

# Per-source state-machine labels (mutually exclusive).
STATUS_BUILT = "built-needs-upload"
STATUS_REUSED = "reused-local-needs-upload"
STATUS_PUBLISHED = "already-published"
STATUS_FAILED = "failed"

# Files that the LFS threshold applies to in the default Hub verifier.
ExportRecordLike = ExportRecord


class OrchestratorError(RuntimeError):
    """Raised for orchestrator-level failures after preflight succeeds."""


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


def _write_publication_state(*args: Any, **kwargs: Any) -> dict[str, object]:
    try:
        return _state_write_publication_state(*args, **kwargs)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _metadata_state_matches(*args: Any, **kwargs: Any) -> bool:
    try:
        return _state_metadata_state_matches(*args, **kwargs)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


def _write_metadata_state(*args: Any, **kwargs: Any) -> dict[str, object]:
    try:
        return _state_write_metadata_state(*args, **kwargs)
    except PublicationStateError as error:
        raise _translate_state_error(error) from error


@dataclass
class SourceOutcome:
    source_name: str
    status: str
    included_rows: int = 0
    output_bytes: int = 0
    remote_revision: str | None = None
    note: str | None = None


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
# Per-source processing
# ---------------------------------------------------------------------------


def _local_artifact_is_complete(paths: Paths, source: Source) -> tuple[bool, Manifest | None]:
    """Return (True, manifest) only when the local artifact is fully resumable."""
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


def _process_one(
    source: Source,
    paths: Paths,
    *,
    clock: Callable[[], str],
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    source_index: int = 0,
    source_total: int = 0,
    osmium_executable: str = "osmium",
) -> SourceOutcome:
    """Build or reuse one PBF and return the resulting per-source state."""
    complete, manifest = _local_artifact_is_complete(paths, source)
    state = read_publication_state(paths.data_root)
    published = cast_dict(state.get("published", {}))
    existing = cast_dict(published.get(source.name, {}))
    output_path = paths.data_root / "data" / source.output_name

    if (
        complete
        and manifest is not None
        and _published_state_matches(existing, manifest, source, output_path)
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
        generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
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
    if logger is not None:

        def _on_progress(emitted: int, included: int) -> None:
            assert logger is not None
            logger.event(
                "build_progress",
                level="INFO",
                source=source.name,
                source_index=source_index,
                source_total=source_total,
                emitted=emitted,
                included=included,
            )

        progress_callback = _on_progress
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
    generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
    return SourceOutcome(
        source_name=source.name,
        status=STATUS_BUILT,
        included_rows=result.included_rows,
        output_bytes=result.output_path.stat().st_size,
        remote_revision=None,
        note="freshly built; upload required",
    )


def _published_state_matches(
    existing: dict[str, object],
    manifest: Manifest,
    source: Source,
    output_path: Path,
) -> bool:
    source_identity = source_identity_for(source.path)
    output_identity = output_identity_for(output_path)
    if existing.get("source_sha256") != source_identity.sha256:
        return False
    if existing.get("output_sha256") != output_identity.sha256:
        return False
    if manifest.source != source_identity:
        return False
    return manifest.output == output_identity


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
    try:
        if upload_runner is None:
            execute_upload(
                plan,
                confirmation=plan.identity_sha256,
                timeout=timeout,
                retry_observer=(
                    None
                    if logger is None
                    else lambda **fields: logger.event("upload_retry", **fields)
                ),
            )
        else:
            command = per_pbf_command(paths.data_root, source.name)
            revision = upload_runner(command)
            if not revision:
                raise PublicationError("upload runner returned empty revision")
    except KeyboardInterrupt:
        raise
    except (PublicationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OrchestratorError(f"upload failed for {source.name}: {error}") from error

    if verifier is None:
        raise OrchestratorError("no Hub verifier supplied; refusing to record an unknown revision")
    if logger is not None:
        logger.event("upload_complete", level="INFO", source=source.name)
        logger.event("verification_start", level="INFO", source=source.name)
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
    if logger is not None:
        logger.event(
            "verification_complete",
            level="INFO",
            source=source.name,
            verified_revision=verified,
        )
    return verified


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
    exporter: Callable[..., Iterable[ExportRecordLike]] | None = None,
    verifier: HubVerifier | None = None,
    verifier_factory: Callable[[], HubVerifier] | None = None,
    upload_timeout: float | None = None,
    subprocess_runner: Callable[[list[str]], None] | None = None,
    progress_interval: int = 100_000,
    logger: RunLogger | None = None,
    osmium_executable: str = "osmium",
) -> OrchestrationReport:
    """Stoppable, resumable build + publish for every discovered PBF.

    The default production path uses the real Hugging Face Hub API for
    verification and ``subprocess.run`` for the upload. Tests may inject
    ``upload_runner`` (a callable receiving the canonical command) and/or
    ``verifier`` (a callable returning a SHA on success) without exposing
    any of these via the public CLI.
    """
    if clock is None:
        clock = _default_clock
    owns_logger = logger is None
    if logger is None:
        if paths is None:
            if data_root is None:
                raise OrchestratorError("logger requires paths or data_root")
            data_root_path = data_root
        else:
            data_root_path = paths.data_root
        logger = RunLogger(
            data_root=data_root_path,
            run_id=str(uuid.uuid4()),
            clock=clock,
            buffer_preflight=True,
        )
    try:
        if subprocess_runner is not None:
            # Tests may redirect the production subprocess boundary; install it
            # by monkeypatching the default runner used inside execute_upload.
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
                _ = (
                    max_retries,
                    backoff_seconds,
                    backoff_factor,
                    backoff_cap_seconds,
                    timeout,
                    _runner,
                    retry_observer,
                )

            pub._default_runner_with_retry = _bridge
            try:
                return _run_and_publish(
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
                    osmium_executable=osmium_executable,
                )
            finally:
                pub._default_runner_with_retry = original_runner

        return _run_and_publish(
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
            osmium_executable=osmium_executable,
        )
    except KeyboardInterrupt:
        logger.event("interrupted", level="WARNING", stage="run-and-publish")
        raise
    finally:
        if owns_logger:
            logger.close()


def _run_and_publish(
    *,
    source_root: Path | None,
    data_root: Path | None,
    confirm_repo: str,
    preflight: Preflight | None,
    upload_runner: Callable[[list[str]], str] | None,
    clock: Callable[[], str],
    paths: Paths | None,
    exporter: Callable[..., Iterable[ExportRecordLike]] | None,
    verifier: HubVerifier | None,
    verifier_factory: Callable[[], HubVerifier] | None,
    upload_timeout: float | None,
    progress_interval: int,
    logger: RunLogger,
    osmium_executable: str,
) -> OrchestrationReport:
    if paths is None:
        if source_root is None or data_root is None:
            raise OrchestratorError("paths or (source_root, data_root) is required")
        paths = Paths(source_root=source_root, data_root=data_root)

    try:
        if preflight is None:
            preflight_report = default_preflight(
                paths,
                confirm_repo=confirm_repo,
                osmium_executable=osmium_executable,
                hf_executable="hf",
            )
        else:
            preflight_report = preflight()
    except Exception as error:
        logger.event("preflight_denied", level="ERROR", reason=str(error))
        logger.deny_preflight()
        raise
    logger.event(
        "preflight",
        level="INFO",
        osmium_executable=preflight_report.get("osmium_executable", "osmium"),
        osmium_version=preflight_report.get("osmium_version", ""),
        hub_repo_sha=preflight_report.get("hub_repo_sha", ""),
        source_count=preflight_report.get("source_count", 0),
    )
    logger.approve_preflight()
    removed_temps = cleanup_stale_owned_temps(paths.data_root)
    if removed_temps:
        logger.event("stale_temp_cleanup", level="INFO", rows=len(removed_temps))

    sources = discover_sources(paths.source_root)
    report = OrchestrationReport(source_count=len(sources), preflight=preflight_report)
    total_sources = len(sources)
    logger.event(
        "sources_discovered",
        level="INFO",
        total=total_sources,
    )

    # Resolve the verifier exactly once so a single HfApi instance is used.
    active_verifier: HubVerifier | None = verifier
    if active_verifier is None and verifier_factory is not None:
        active_verifier = verifier_factory()
    elif active_verifier is None and verifier is None and upload_runner is None:
        # Production path uses the default Hub verifier factory.
        try:
            active_verifier = default_hub_verifier_factory(
                cache_dir=paths.data_root / ".cache" / "huggingface" / "hub"
            )
        except TypeError as error:
            # Compatibility for injected legacy no-argument factories.
            if "unexpected keyword argument" not in str(error):
                raise
            active_verifier = default_hub_verifier_factory()

    per_pbf_upload_count = 0
    for index, source in enumerate(sources, start=1):
        outcome = _process_one(
            source,
            paths,
            clock=clock,
            exporter=exporter,
            progress_interval=progress_interval,
            logger=logger,
            source_index=index,
            source_total=total_sources,
            osmium_executable=osmium_executable,
        )
        if outcome.status == STATUS_PUBLISHED:
            report.outcomes.append(outcome)
            continue
        if outcome.status in {STATUS_BUILT, STATUS_REUSED}:
            logger.event(
                "upload_start",
                level="INFO",
                source=source.name,
                source_index=index,
                source_total=total_sources,
            )
            try:
                revision = _execute_publication(
                    paths,
                    source,
                    verifier=active_verifier,
                    timeout=upload_timeout,
                    upload_runner=upload_runner,
                    logger=logger,
                )
            except OrchestratorError as error:
                outcome.status = STATUS_FAILED
                outcome.note = str(error)
                report.outcomes.append(outcome)
                logger.event(
                    "upload_failed",
                    level="ERROR",
                    source=source.name,
                    source_index=index,
                    source_total=total_sources,
                    reason=str(error),
                )
                raise
            output_path = paths.data_root / "data" / source.output_name
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
                source_index=index,
                source_total=total_sources,
            )
            outcome.remote_revision = revision
            outcome.note = "published after verified upload"
            per_pbf_upload_count += 1
        report.outcomes.append(outcome)

    _verify_final_completeness(paths, sources)

    # Refresh the canonical README, stats, and H3 map before constructing
    # the dataset-wide reconciliation plan. This ordering is required for
    # migration of a completed pre-H3 dataset: reconciliation must see the
    # same complete allowlisted surface that metadata publication will see.
    # The byte-stable writers preserve a true no-op when nothing changed.
    _refresh_dataset_docs_for_metadata(paths, clock=clock, logger=logger)

    # The production verifier owns a narrowly scoped reconciliation hook.
    # It removes remote artifacts that no longer exist locally, but only
    # below data/ and manifests/; unrelated repository files are preserved.
    reconcile = getattr(active_verifier, "reconcile_managed_files", None)
    if callable(reconcile):
        full_plan = create_upload_plan(paths.data_root)
        logger.event("remote_reconciliation_start", level="INFO")
        try:
            revision = reconcile(
                REPO_ID,
                {item.relative_path for item in full_plan.files},
            )
        except KeyboardInterrupt:
            raise
        except Exception as error:
            raise OrchestratorError(f"remote artifact reconciliation failed: {error}") from error
        logger.event(
            "remote_reconciliation_complete",
            level="INFO",
            verified_revision=str(revision or ""),
        )

    # Final metadata is published independently. The metadata is uploaded
    # whenever its current plan identity differs from the verified
    # metadata state. This is independent of per-PBF upload count, so a
    # failed intermediate metadata upload is retried on the next run.
    final_metadata_revision = _upload_final_metadata(
        paths,
        verifier=active_verifier,
        upload_runner=upload_runner,
        upload_timeout=upload_timeout,
        clock=clock,
        logger=logger,
    )
    if final_metadata_revision is not None:
        report.final_remote_revision = final_metadata_revision
    logger.event(
        "run_summary",
        level="INFO",
        result="completed",
        source_count=total_sources,
        per_pbf_uploads=per_pbf_upload_count,
    )
    logger.flush()
    return report


def _refresh_dataset_docs_for_metadata(
    paths: Paths,
    *,
    clock: Callable[[], str],
    logger: RunLogger,
) -> None:
    """Refresh the canonical ``README.md``, ``stats.json``, and H3 map.

    The refresh is byte-stable: identical inputs produce identical
    outputs, so the underlying atomic write-if-changed helpers leave
    the existing files untouched on disk. This step exists so that a
    dataset that was published before the H3 map feature exists
    (pre-feature ``README.md``, no ``assets/`` directory) is repaired
    in the next run: the canonical card is regenerated and the H3 map
    is rendered, even when no per-PBF artifacts changed.

    The refresh is also called for normal runs so that the metadata
    plan always reflects the current validated dataset.
    """
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        # No data root to refresh from; the metadata plan will be skipped
        # by ``_upload_final_metadata``.
        return
    try:
        generate_dataset_docs(paths.data_root, dataset_card_template(), clock=clock)
    except Exception as error:
        raise OrchestratorError(f"dataset card refresh failed: {error}") from error
    logger.event(
        "dataset_docs_refreshed",
        level="INFO",
        readme=paths.data_root / "README.md",
        stats=paths.data_root / "stats.json",
        assets=paths.data_root / "assets" / "description_polygon_density.png",
    )


def _verify_final_completeness(paths: Paths, sources: Iterable[Source]) -> None:
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


def _upload_final_metadata(
    paths: Paths,
    *,
    verifier: HubVerifier | None,
    upload_runner: Callable[[list[str]], str] | None,
    upload_timeout: float | None = None,
    clock: Callable[[], str] | None = None,
    logger: RunLogger | None = None,
) -> str | None:
    """Upload README.md + stats.json and verify the result via Hub API.

    The metadata is uploaded only when its current plan identity differs
    from the recorded, verified metadata state. The metadata state is
    written atomically only after remote verification succeeds.

    Raises :class:`OrchestratorError` on any failure. Returns the verified
    remote revision.
    """
    if clock is None:
        clock = _default_clock
    data_dir = paths.data_root / "data"
    if not data_dir.is_dir() or not list(data_dir.glob("*.parquet")):
        return None
    metadata_plan = _build_metadata_only_upload_plan(paths.data_root)
    # Revalidate the dataset-wide upload plan immediately before upload to
    # catch in-place mutations between per-PBF uploads and final metadata.
    create_upload_plan(paths.data_root)

    # Independently resumable: if the verified metadata state matches the
    # current plan identity, skip the upload entirely.
    if _metadata_state_matches(paths.data_root, metadata_plan):
        state_payload = read_publication_state(paths.data_root)
        metadata_state = cast_dict(state_payload.get("metadata", {}))
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
            from osm_polygon_description_tag.publication.planning import metadata_only_command

            command = metadata_only_command(paths.data_root)
            revision = upload_runner(command)
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

    # Write the metadata state atomically only after verification succeeds.
    from osm_polygon_description_tag.dataset.manifest import file_sha256
    from osm_polygon_description_tag.publication.planning import (
        AREA_HISTOGRAM_ASSET_RELATIVE,
        H3_MAP_ASSET_RELATIVE,
    )

    map_path = paths.data_root / H3_MAP_ASSET_RELATIVE
    histogram_path = paths.data_root / AREA_HISTOGRAM_ASSET_RELATIVE
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
