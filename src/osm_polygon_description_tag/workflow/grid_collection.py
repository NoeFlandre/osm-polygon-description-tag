"""Import retrieved results and acknowledge them against the recorded intent."""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardPaths,
    ShardStatus,
    exclusive_worker_lock,
    read_checkpoint,
    read_receipt,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotError,
    read_snapshot,
)
from osm_polygon_description_tag.dataset.languages.validation import (
    RunReport,
    ShardReport,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.runtime.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_via,
)
from osm_polygon_description_tag.workflow.grid_models import (
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    _validate_relative_parquet,
    job_paths,
    read_intent,
)
from osm_polygon_description_tag.workflow.grid_prepare import (
    _existing_bundle,
)
from osm_polygon_description_tag.workflow.grid_scheduler import (
    JobState,
)
from osm_polygon_description_tag.workflow.grid_staging import (
    _copy_regular_file,
    _copy_snapshot,
)
from osm_polygon_description_tag.workflow.grid_state import (
    collect_results,
    submission_lock,
)


def import_retrieved_results(local_run_dir: Path, retrieved_run_dir: Path, shard: str) -> RunReport:
    """Validate retrieved state and commit its checkpoint last under both locks."""
    normalized_shard = _validate_relative_parquet(shard, "shard")
    _require_regular_directory(retrieved_run_dir, "retrieved run directory")
    with submission_lock(local_run_dir), exclusive_worker_lock(local_run_dir):
        return _import_retrieved_results(local_run_dir, retrieved_run_dir, normalized_shard)


def _import_retrieved_results(
    local_run_dir: Path, retrieved_run_dir: Path, normalized_shard: str
) -> RunReport:
    _require_matching_snapshots(local_run_dir, retrieved_run_dir)
    incoming_state = shard_paths(retrieved_run_dir, normalized_shard).root
    target = shard_paths(local_run_dir, normalized_shard).root
    _reject_overlapping_paths(incoming_state, target)
    with _staged_collection(local_run_dir, incoming_state, normalized_shard) as staged:
        incoming_report = collect_results(staged, normalized_shard)
        if incoming_report.shards[0].issues:
            raise GridOperatorError(
                f"retrieved {normalized_shard} has collection validation issues"
            )
        local_paths = shard_paths(local_run_dir, normalized_shard)
        staged_paths = shard_paths(staged, normalized_shard)
        first_new_part = _require_forward_progress(local_paths, staged_paths)
        _commit_retrieved_state(local_paths, staged_paths, first_new_part)
    report = collect_results(local_run_dir, normalized_shard)
    _require_clean_import_report(report)
    return report


def _require_forward_progress(local: ShardPaths, incoming: ShardPaths) -> int:
    if not local.checkpoint.exists() and not local.checkpoint.is_symlink():
        return 0
    previous = read_checkpoint(local.checkpoint)
    proposed = read_checkpoint(incoming.checkpoint)
    if proposed.input_cursor < previous.input_cursor:
        raise GridOperatorError("retrieved checkpoint would regress local progress")
    _require_checkpoint_extension(previous, proposed)
    _require_matching_committed_history(local, incoming, previous)
    return len(previous.completed_parts)


def _checkpoint_binding(checkpoint: ShardCheckpoint) -> tuple[str, str, str, int, int]:
    return (
        checkpoint.snapshot_id,
        checkpoint.model_config_fingerprint,
        checkpoint.shard,
        checkpoint.batch_size,
        checkpoint.input_row_count,
    )


def _require_checkpoint_extension(previous: ShardCheckpoint, proposed: ShardCheckpoint) -> None:
    if _checkpoint_binding(previous) != _checkpoint_binding(proposed):
        raise GridOperatorError("retrieved checkpoint changes the committed history binding")


def _require_matching_committed_history(
    local: ShardPaths, incoming: ShardPaths, previous: ShardCheckpoint
) -> None:
    count = 0
    for part in previous.completed_parts:
        _require_identical_artifact(local.part(part), incoming.part(part))
        _require_identical_artifact(local.receipt(part), incoming.receipt(part))
        count += read_receipt(incoming.receipt(part)).row_count
    if count != previous.annotation_count:
        raise GridOperatorError("local annotation count differs from committed history")


def _require_identical_artifact(local: Path, incoming: Path) -> None:
    if local.is_symlink() or not local.is_file():
        raise GridOperatorError(f"local committed history artifact is missing or unsafe: {local}")
    if file_sha256(local) != file_sha256(incoming):
        raise GridOperatorError(f"retrieved results conflict with committed history: {local}")


def _require_matching_snapshots(local_run_dir: Path, retrieved_run_dir: Path) -> None:
    try:
        local_snapshot = read_snapshot(local_run_dir)
        retrieved_snapshot = read_snapshot(retrieved_run_dir)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error
    if local_snapshot != retrieved_snapshot:
        raise GridOperatorError("retrieved results belong to a different snapshot")


def _require_clean_import_report(report: RunReport) -> None:
    if report.issues or any(shard_report.issues for shard_report in report.shards):
        raise GridOperatorError("imported results failed local collection validation")


def _require_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise GridOperatorError(f"{label} is not a regular directory: {path}")


def _reject_overlapping_paths(source: Path, destination: Path) -> None:
    try:
        source_resolved = source.resolve(strict=True)
    except OSError as error:
        raise GridOperatorError(f"retrieved shard directory is unavailable: {source}") from error
    destination_resolved = destination.resolve()
    if (
        source_resolved == destination_resolved
        or source_resolved.is_relative_to(destination_resolved)
        or destination_resolved.is_relative_to(source_resolved)
    ):
        raise GridOperatorError("retrieved and local shard directories must be distinct")


@contextmanager
def _staged_collection(local_run: Path, source: Path, shard: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=".collection-", dir=local_run) as temporary:
        staged = Path(temporary)
        _copy_snapshot(local_run, staged)
        _copy_tree_no_symlinks(source, shard_paths(staged, shard).root)
        yield staged


def _commit_retrieved_state(local: ShardPaths, staged: ShardPaths, first_new_part: int) -> None:
    _prepare_collection_directories(local)
    checkpoint = read_checkpoint(staged.checkpoint)
    for part in checkpoint.completed_parts[first_new_part:]:
        _commit_staged_artifact(staged.part(part), local.part(part))
        _commit_staged_artifact(staged.receipt(part), local.receipt(part))
    atomic_write_bytes(local.checkpoint, staged.checkpoint.read_bytes())


def _prepare_collection_directories(paths: ShardPaths) -> None:
    for directory in (paths.root, paths.parts, paths.receipts):
        if directory.is_symlink():
            raise GridOperatorError(f"local result directory must not be a symlink: {directory}")
        directory.mkdir(parents=True, exist_ok=True)


def _commit_staged_artifact(source: Path, destination: Path) -> None:
    atomic_write_via(destination, lambda temporary: os.replace(source, temporary))


def _copy_tree_no_symlinks(source: Path, destination: Path) -> None:
    _require_regular_directory(source, "source directory")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        _copy_tree_entry(path, destination / path.name)


def _copy_tree_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise GridOperatorError(f"retrieved results contain a symlink: {source}")
    if source.is_dir():
        _copy_tree_no_symlinks(source, destination)
    elif source.is_file():
        _copy_regular_file(source, destination)
    else:
        raise GridOperatorError(f"retrieved results contain a non-file: {source}")


def adopt_retrieved_intent(
    paths: JobPaths, retrieved_run_dir: Path, bundle: JobBundle
) -> Path | None:
    """Bring the site's record of a submission into the owned run directory.

    A bounded job is submitted from the site's frontend against the site's copy
    of the run, so the durable intent is written there rather than here.
    Collection retrieves that copy; adopting its intent is what lets the owned
    run acknowledge the shard instead of seeing a bundle nothing ever claimed
    to submit.

    An intent already recorded here always wins. It is this run's own record,
    and it may already carry an acknowledgment that a retrieved copy predates.
    """
    if _owns_an_intent_already(paths):
        return None
    intent = _bound_retrieved_intent(job_paths(retrieved_run_dir, bundle).intent, bundle)
    with submission_lock(paths.run_dir):
        if paths.intent.exists():
            return None
        paths.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.intent, intent.to_payload())
        return paths.intent


def _owns_an_intent_already(paths: JobPaths) -> bool:
    if paths.intent.is_symlink():
        raise GridOperatorError(f"submission intent must be a regular file: {paths.intent}")
    return paths.intent.exists()


def _bound_retrieved_intent(incoming: Path, bundle: JobBundle) -> SubmissionIntent:
    intent = _retrieved_intent(incoming)
    if intent.bundle_id != bundle.bundle_id or intent.shard != bundle.shard:
        raise GridOperatorError(
            f"retrieved submission intent is not bound to this bundle: {incoming}"
        )
    return intent


def _retrieved_intent(incoming: Path) -> SubmissionIntent:
    if incoming.is_symlink() or (incoming.exists() and not incoming.is_file()):
        raise GridOperatorError(f"submission intent must be a regular file: {incoming}")
    if not incoming.exists():
        raise GridOperatorError(f"retrieved run has no submission intent: {incoming}")
    return read_intent(incoming)


def acknowledge_collected_results(
    paths: JobPaths, report: RunReport, *, now: datetime | None = None
) -> SubmissionIntent:
    """Durably acknowledge one validated terminal result for a shard."""
    selected = _acknowledgment_shard(report)
    with submission_lock(paths.run_dir):
        intent, bundle = _bound_submission(paths)
        _validate_acknowledgment(report, selected, intent, bundle)
        updated = _acknowledge_intent(intent, report, now)
        atomic_write_json(paths.intent, updated.to_payload())
        return updated


def _acknowledgment_shard(report: RunReport) -> ShardReport:
    if not isinstance(report, RunReport) or len(report.shards) != 1:
        raise GridOperatorError("collection acknowledgment requires one shard report")
    return report.shards[0]


def _bound_submission(paths: JobPaths) -> tuple[SubmissionIntent, JobBundle]:
    intent = read_intent(paths.intent)
    bundle = _existing_bundle(paths)
    if bundle is None:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    if intent.bundle_id != bundle.bundle_id or intent.shard != bundle.shard:
        raise GridOperatorError("submission intent is not bound to the prepared bundle")
    return intent, bundle


def _validate_acknowledgment(
    report: RunReport,
    selected: ShardReport,
    intent: SubmissionIntent,
    bundle: JobBundle,
) -> None:
    _validate_ack_identity(report, selected, bundle)
    _validate_ack_report_clean(report, selected)
    _validate_ack_terminal(selected, intent)
    if intent.result_complete and not report.is_complete:
        raise GridOperatorError("complete results cannot be downgraded to paused results")


def _validate_ack_identity(report: RunReport, selected: ShardReport, bundle: JobBundle) -> None:
    if report.snapshot_id != bundle.snapshot_id:
        raise GridOperatorError("collected results do not match the submitted bundle")
    if selected.shard != bundle.shard:
        raise GridOperatorError("collected results do not match the submitted bundle")


def _validate_ack_report_clean(report: RunReport, selected: ShardReport) -> None:
    if report.issues or selected.issues:
        raise GridOperatorError("cannot acknowledge collection validation issues")


def _validate_ack_terminal(selected: ShardReport, intent: SubmissionIntent) -> None:
    if selected.status not in {str(ShardStatus.PAUSED), str(ShardStatus.COMPLETE)}:
        raise GridOperatorError("collected results do not have a terminal checkpoint")
    if intent.terminal_state != str(JobState.TERMINATED):
        raise GridOperatorError("collected results require a terminal scheduler state")


def _acknowledge_intent(
    intent: SubmissionIntent, report: RunReport, now: datetime | None
) -> SubmissionIntent:
    return replace(
        intent,
        result_acknowledged=True,
        result_complete=report.is_complete,
        collected_at=(now or datetime.now(UTC)).isoformat(),
    )
