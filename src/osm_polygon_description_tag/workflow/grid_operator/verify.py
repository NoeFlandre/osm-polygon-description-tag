"""Grid operator verify responsibilities."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.atomic import (
    atomic_write_bytes,
    atomic_write_via,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    ShardCheckpoint,
    ShardPaths,
    exclusive_worker_lock,
    read_checkpoint,
    read_receipt,
    shard_paths,
)
from osm_polygon_description_tag.dataset.languages.payloads import PayloadReader, require_object
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SNAPSHOT_FILENAME,
    SnapshotError,
    SnapshotManifest,
    fingerprint_lockfile,
    fingerprint_project_source,
    read_snapshot,
    verify_source_file,
)
from osm_polygon_description_tag.dataset.languages.validation import RunReport
from osm_polygon_description_tag.dataset.manifest import file_sha256

from .bundle import _read_job_config, _read_json, read_bundle
from .models import (
    BUNDLE_FILENAME,
    JOB_CONFIG_FILENAME,
    JOB_SCRIPT_FILENAME,
    STAGE_MANIFEST_FILENAME,
    STAGE_PROJECT_DIRNAME,
    STAGE_RUN_DIRNAME,
    STAGE_SCHEMA_VERSION,
    STAGE_SOURCE_DIRNAME,
    GridOperatorError,
    JobBundle,
    PreparedJob,
    StagedFile,
    _validate_fingerprint,
    _validate_relative_parquet,
    _validate_stage_relative_file,
)
from .reconciliation import collect_results
from .script import _validate_remote_path
from .stage import (
    _copy_regular_file,
    _copy_snapshot,
    _payload_files,
    _project_source_root,
    _resume_state_fingerprint,
    _validate_resume_state,
)
from .state import submission_lock


def verify_prepared_bundle(
    payload_root: Path, *, expected_bundle: JobBundle | None = None
) -> JobBundle:
    """Verify every staged byte and identity before a compute job can run."""
    files = _payload_files(payload_root)
    bundle = _read_expected_bundle(payload_root, expected_bundle)
    stage_path, stage = _read_stage_manifest(payload_root, bundle)
    descriptors, resume_fingerprint = _stage_descriptors(stage)
    listed = _verify_stage_file_listing(payload_root, files, stage_path, descriptors)
    _verify_required_payload(payload_root, bundle, listed)
    _verify_resume_binding(payload_root, bundle, resume_fingerprint)
    return bundle


def _read_expected_bundle(payload_root: Path, expected_bundle: JobBundle | None) -> JobBundle:
    bundle = read_bundle(payload_root / BUNDLE_FILENAME)
    if expected_bundle is not None and bundle != expected_bundle:
        raise GridOperatorError("staged bundle does not match the expected bundle")
    return bundle


def _read_stage_manifest(payload_root: Path, bundle: JobBundle) -> tuple[Path, PayloadReader]:
    stage_path = payload_root / STAGE_MANIFEST_FILENAME
    if stage_path.is_symlink() or not stage_path.is_file():
        raise GridOperatorError("portable payload is missing its stage manifest")
    stage = require_object(
        _read_json(stage_path, "stage manifest"), error=GridOperatorError, label="stage"
    )
    if stage.integer("stage_schema_version") != STAGE_SCHEMA_VERSION:
        raise GridOperatorError("unsupported stage schema version")
    if stage.text("bundle_id") != bundle.bundle_id:
        raise GridOperatorError("stage manifest bundle id does not match bundle")
    return stage_path, stage


def _stage_descriptors(stage: PayloadReader) -> tuple[tuple[StagedFile, ...], str]:
    resume_fingerprint = stage.text("resume_state_fingerprint")
    _validate_fingerprint(resume_fingerprint, "resume state fingerprint")
    descriptors = tuple(StagedFile.from_payload(item) for item in stage.items("files"))
    return descriptors, resume_fingerprint


def _verify_stage_file_listing(
    payload_root: Path,
    files: tuple[Path, ...],
    stage_path: Path,
    descriptors: tuple[StagedFile, ...],
) -> set[str]:
    listed = _stage_descriptor_names(descriptors)
    actual = _payload_file_names(payload_root, files, stage_path)
    _verify_stage_file_names(actual, listed)
    _verify_staged_files(payload_root, descriptors)
    return set(listed)


def _stage_descriptor_names(descriptors: tuple[StagedFile, ...]) -> tuple[str, ...]:
    return tuple(item.relative_path for item in descriptors)


def _payload_file_names(
    payload_root: Path, files: tuple[Path, ...], stage_path: Path
) -> tuple[str, ...]:
    return tuple(path.relative_to(payload_root).as_posix() for path in files if path != stage_path)


def _verify_stage_file_names(actual: tuple[str, ...], listed: tuple[str, ...]) -> None:
    if len(set(listed)) != len(listed):
        raise GridOperatorError("stage manifest contains duplicate files")
    if set(actual) != set(listed):
        raise GridOperatorError("stage manifest does not account for every payload file")


def _verify_staged_files(payload_root: Path, descriptors: tuple[StagedFile, ...]) -> None:
    for descriptor in descriptors:
        _verify_staged_file(payload_root, descriptor)


def _verify_staged_file(payload_root: Path, descriptor: StagedFile) -> None:
    path = _payload_file(payload_root, descriptor.relative_path)
    if path.stat().st_size != descriptor.size_bytes or file_sha256(path) != descriptor.sha256:
        raise GridOperatorError(f"staged file hash does not match: {descriptor.relative_path}")


def _verify_resume_binding(payload_root: Path, bundle: JobBundle, expected: str) -> None:
    actual = _resume_state_fingerprint(payload_root / STAGE_RUN_DIRNAME, bundle.shard)
    if actual != expected:
        raise GridOperatorError("stage manifest resume state fingerprint does not match payload")


def _payload_file(root: Path, relative_path: str) -> Path:
    normalized = _validate_stage_relative_file(relative_path)
    current = root
    for part in Path(normalized).parts:
        current /= part
        if current.is_symlink():
            raise GridOperatorError(f"staged path contains a symlink: {relative_path}")
    if not current.is_file():
        raise GridOperatorError(f"staged file is missing: {relative_path}")
    return current


def _verify_required_payload(payload_root: Path, bundle: JobBundle, listed: set[str]) -> None:
    _require_payload_files(bundle, listed)
    project_root, source_root, run_root = _payload_directories(payload_root)
    _verify_payload_layout(payload_root, project_root, bundle)
    _verify_payload_identity(project_root, source_root, run_root, bundle)
    _verify_one_staged_input(source_root, bundle)
    _verify_staged_resume(run_root, bundle.shard)


def _require_payload_files(bundle: JobBundle, listed: set[str]) -> None:
    required = {
        BUNDLE_FILENAME,
        JOB_CONFIG_FILENAME,
        JOB_SCRIPT_FILENAME,
        f"{STAGE_PROJECT_DIRNAME}/pyproject.toml",
        f"{STAGE_PROJECT_DIRNAME}/uv.lock",
        f"{STAGE_SOURCE_DIRNAME}/{bundle.shard}",
        f"{STAGE_RUN_DIRNAME}/{SNAPSHOT_FILENAME}",
    }
    if not required.issubset(listed):
        missing = sorted(required - listed)
        raise GridOperatorError(f"portable payload is missing required files: {', '.join(missing)}")


def _payload_directories(payload_root: Path) -> tuple[Path, Path, Path]:
    directories = (
        payload_root / STAGE_PROJECT_DIRNAME,
        payload_root / STAGE_SOURCE_DIRNAME,
        payload_root / STAGE_RUN_DIRNAME,
    )
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise GridOperatorError(f"portable payload directory is invalid: {directory}")
    return directories


def _verify_payload_layout(payload_root: Path, project_root: Path, bundle: JobBundle) -> None:
    source_root = _project_source_root(project_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise GridOperatorError("portable payload is missing project source code")
    if not os.access(payload_root / JOB_SCRIPT_FILENAME, os.X_OK):
        raise GridOperatorError("portable job script is not executable")
    _read_job_config(payload_root / JOB_CONFIG_FILENAME, bundle)


def _verify_payload_identity(
    project_root: Path, source_root: Path, run_root: Path, bundle: JobBundle
) -> None:
    try:
        staged_snapshot = read_snapshot(run_root)
        _validate_staged_snapshot(staged_snapshot, bundle)
        verify_source_file(staged_snapshot, source_root, bundle.shard)
        _validate_staged_project(project_root, bundle)
    except SnapshotError as error:
        raise GridOperatorError(str(error)) from error


def _validate_staged_snapshot(snapshot: SnapshotManifest, bundle: JobBundle) -> None:
    if snapshot.snapshot_id != bundle.snapshot_id:
        raise GridOperatorError("staged snapshot id does not match bundle")
    if snapshot.code_fingerprint != bundle.code_fingerprint:
        raise GridOperatorError("staged snapshot code fingerprint does not match bundle")
    if snapshot.lock_fingerprint != bundle.lock_fingerprint:
        raise GridOperatorError("staged snapshot lock fingerprint does not match bundle")


def _validate_staged_project(project_root: Path, bundle: JobBundle) -> None:
    if fingerprint_project_source(project_root) != bundle.code_fingerprint:
        raise GridOperatorError("staged project source does not match bundle")
    if fingerprint_lockfile(project_root) != bundle.lock_fingerprint:
        raise GridOperatorError("staged lockfile does not match bundle")


def _verify_one_staged_input(source_root: Path, bundle: JobBundle) -> None:
    source_files = tuple(_payload_files(source_root))
    if tuple(path.relative_to(source_root).as_posix() for path in source_files) != (bundle.shard,):
        raise GridOperatorError("portable payload must contain exactly one input shard")


def _verify_staged_resume(run_root: Path, shard: str) -> None:
    _validate_resume_state(run_root, shard)


def build_bundle_transfer_argv(prepared: PreparedJob, remote_bundle_dir: str) -> tuple[str, ...]:
    """Return an explicit argv that transfers a verified bundle directory."""
    if not isinstance(prepared, PreparedJob):
        raise GridOperatorError("prepared bundle must be a PreparedJob")
    verify_prepared_bundle(prepared.payload_root, expected_bundle=prepared.bundle)
    _validate_remote_path(remote_bundle_dir, "remote bundle directory")
    destination = remote_bundle_dir.rstrip("/") or "/"
    return (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        f"{prepared.payload_root}/",
        f"{destination}/",
    )


def build_result_retrieval_argv(
    remote_run_dir: str, local_staging_dir: Path, shard: str
) -> tuple[str, ...]:
    """Return an explicit argv for retrieving one validated shard directory."""
    _validate_remote_path(remote_run_dir, "remote run directory")
    normalized_shard = _validate_relative_parquet(shard, "shard")
    if local_staging_dir.is_symlink() or (
        local_staging_dir.exists() and not local_staging_dir.is_dir()
    ):
        raise GridOperatorError(
            f"local staging directory is not a regular directory: {local_staging_dir}"
        )
    destination = shard_paths(local_staging_dir, normalized_shard).root
    remote_base = remote_run_dir.rstrip("/") or "/"
    remote_source = f"{remote_base}/shards/{destination.name}/"
    return (
        "rsync",
        "--archive",
        "--checksum",
        "--protect-args",
        "--",
        remote_source,
        f"{destination}/",
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
