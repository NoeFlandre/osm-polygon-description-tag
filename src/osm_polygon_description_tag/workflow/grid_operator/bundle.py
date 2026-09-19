"""Grid operator bundle responsibilities."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.atomic import (
    atomic_write_bytes,
    atomic_write_json,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    ShardCheckpoint,
    ShardPaths,
    ShardStatus,
    exclusive_worker_lock,
    is_generated_part_name,
    is_generated_receipt_name,
    read_checkpoint,
    receipt_name_for_part,
    shard_paths,
    write_checkpoint,
)
from osm_polygon_description_tag.dataset.languages.payloads import require_object
from osm_polygon_description_tag.dataset.languages.snapshot import SnapshotManifest
from osm_polygon_description_tag.workflow.grid_policy import (
    MAX_PROCESSING_SECONDS,
    MAX_WALLTIME_SECONDS,
    REQUIRED_CORES,
)

from .models import (
    JOB_CONFIG_SCHEMA_VERSION,
    QUARANTINE_DIRNAME,
    GridOperatorError,
    JobBundle,
    JobPaths,
    SubmissionIntent,
    bundle_for_shard,
)
from .script import (
    _glotlid_model_path_for_snapshot,
    _validate_batch_size,
    _validate_job_limits,
    job_paths,
    render_job_script,
)
from .state import (
    fsync_directory,
    resume_checkpoint_is_present,
    resume_root_has_children,
    submission_lock,
    validate_resume_layout,
    validate_resume_root,
)


def prepare_job(
    run_dir: Path,
    snapshot: SnapshotManifest,
    shard: str,
    *,
    remote_project_dir: str,
    remote_source_dir: str,
    remote_run_dir: str,
    processing_seconds: int = MAX_PROCESSING_SECONDS,
    batch_size: int = 512,
    walltime_seconds: int = MAX_WALLTIME_SECONDS,
    remote_bundle_dir: str | None = None,
    sat_model_path: str,
    glotlid_model_path: str | None = None,
    submission_locked: bool = False,
) -> tuple[JobBundle, JobPaths]:
    """Write the immutable bundle, job script, and initial checkpoint of a shard.

    Pass ``submission_locked`` when the caller already holds the run-wide
    submission lock; the worker lock is then the only lock taken here.
    """
    bundle = bundle_for_shard(snapshot, shard)
    paths = job_paths(run_dir, bundle)
    existing = _existing_bundle(paths)
    if existing is not None and existing != bundle:
        raise GridOperatorError("a different bundle is already prepared in this job directory")
    remote_model_path = _glotlid_model_path_for_snapshot(snapshot, glotlid_model_path)
    script = render_job_script(
        bundle,
        remote_project_dir=remote_project_dir,
        remote_source_dir=remote_source_dir,
        remote_run_dir=remote_run_dir,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
        remote_bundle_dir=remote_bundle_dir,
        sat_model_path=sat_model_path,
        glotlid_model_path=remote_model_path,
    )
    config = _job_config_payload(
        bundle,
        processing_seconds=processing_seconds,
        batch_size=batch_size,
        walltime_seconds=walltime_seconds,
    )
    script_bytes = script.encode("utf-8")  # pragma: no mutate - codec alias only
    if existing is None:
        _write_prepared_artifacts(paths, bundle, config, script_bytes)
    else:
        _verify_immutable_prepared_artifacts(paths, config, script_bytes)
    initialize_shard_checkpoint(
        run_dir, bundle, batch_size=batch_size, submission_locked=submission_locked
    )
    return bundle, paths


def _write_prepared_artifacts(
    paths: JobPaths, bundle: JobBundle, config: dict[str, object], script_bytes: bytes
) -> None:
    atomic_write_json(paths.bundle, bundle.to_payload())
    atomic_write_json(paths.config, config)
    atomic_write_bytes(paths.script, script_bytes)
    paths.script.chmod(0o700)


@contextmanager
def _state_locks(run_dir: Path, *, submission_locked: bool) -> Iterator[None]:
    """Hold the submission lock, then the worker lock, in that order."""
    if submission_locked:
        with exclusive_worker_lock(run_dir):
            yield
        return
    with _both_state_locks(run_dir):
        yield


@contextmanager
def _both_state_locks(run_dir: Path) -> Iterator[None]:
    """Hold the run-wide submission lock, then this run's worker lock."""
    with submission_lock(run_dir), exclusive_worker_lock(run_dir):
        yield


def initialize_shard_checkpoint(
    run_dir: Path,
    bundle: JobBundle,
    *,
    batch_size: int,
    submission_locked: bool = False,
) -> ShardCheckpoint:
    """Create, or confirm, the zero-cursor checkpoint of one prepared shard.

    A job that dies during setup then still leaves a validated resumable
    position behind, so its results can be collected and acknowledged. An
    existing checkpoint is never rewritten; it is only checked against the
    bundle it must belong to.
    """
    _validate_batch_size(batch_size)
    with _state_locks(run_dir, submission_locked=submission_locked):
        return _initialized_checkpoint(run_dir, bundle, batch_size)


def _initialized_checkpoint(run_dir: Path, bundle: JobBundle, batch_size: int) -> ShardCheckpoint:
    state = shard_paths(run_dir, bundle.shard)
    validate_resume_root(state)
    existing = _shard_checkpoint(state)
    if existing is not None:
        _require_bound_checkpoint(existing, bundle)
        return existing
    if resume_root_has_children(state):
        raise GridOperatorError("resume artifacts exist without a checkpoint")
    checkpoint = _initial_checkpoint(bundle, batch_size)
    write_checkpoint(state.checkpoint, checkpoint)
    return checkpoint


def _initial_checkpoint(bundle: JobBundle, batch_size: int) -> ShardCheckpoint:
    consumed = bundle.input_row_count == 0
    return ShardCheckpoint(
        snapshot_id=bundle.snapshot_id,
        model_config_fingerprint=bundle.model_config_fingerprint,
        shard=bundle.shard,
        batch_size=batch_size,
        input_row_count=bundle.input_row_count,
        input_cursor=0,
        annotation_count=0,
        completed_parts=(),
        status=ShardStatus.COMPLETE if consumed else ShardStatus.PAUSED,
    )


def _shard_checkpoint(state: ShardPaths) -> ShardCheckpoint | None:
    """Read the shard checkpoint, failing closed on anything unusable."""
    if state.checkpoint.is_symlink():
        raise GridOperatorError("resume checkpoint must not be a symlink")
    if not state.checkpoint.exists():
        return None
    if not state.checkpoint.is_file():
        raise GridOperatorError("resume checkpoint must be a regular file")
    try:
        return read_checkpoint(state.checkpoint)
    except CheckpointError as error:
        raise GridOperatorError(f"shard checkpoint is unusable: {error}") from error


def _checkpoint_identity(checkpoint: ShardCheckpoint) -> tuple[str, str, str, int]:
    return (
        checkpoint.snapshot_id,
        checkpoint.model_config_fingerprint,
        checkpoint.shard,
        checkpoint.input_row_count,
    )


def _bundle_identity(bundle: JobBundle) -> tuple[str, str, str, int]:
    return (
        bundle.snapshot_id,
        bundle.model_config_fingerprint,
        bundle.shard,
        bundle.input_row_count,
    )


def _require_bound_checkpoint(checkpoint: ShardCheckpoint, bundle: JobBundle) -> None:
    if _checkpoint_identity(checkpoint) != _bundle_identity(bundle):
        raise GridOperatorError("existing shard checkpoint is not bound to this job bundle")


def quarantine_orphan_artifacts(run_dir: Path, shard: str) -> tuple[str, ...]:
    """Move generated artifacts the checkpoint does not list out of the way.

    A worker that died between writing a part or receipt and committing its
    checkpoint leaves artifacts the authoritative checkpoint knows nothing
    about. They are never adopted and never deleted: they are preserved under
    ``quarantine/`` so the checkpoint-listed state can be restaged, collected,
    and resumed. Anything that is not a recognized generated artifact keeps
    failing closed.
    """
    with _both_state_locks(run_dir):
        return _quarantined_orphans(run_dir, shard)


def _quarantined_orphans(run_dir: Path, shard: str) -> tuple[str, ...]:
    state = shard_paths(run_dir, shard)
    validate_resume_root(state)
    if not resume_checkpoint_is_present(state):
        return ()
    checkpoint = read_checkpoint(state.checkpoint)
    validate_resume_layout(state)
    orphans = _orphan_artifacts(state, checkpoint)
    for relative in orphans:
        _quarantine_artifact(state, relative)
    return orphans


def _orphan_artifacts(state: ShardPaths, checkpoint: ShardCheckpoint) -> tuple[str, ...]:
    groups = (
        (state.parts, set(checkpoint.completed_parts), is_generated_part_name),
        (
            state.receipts,
            {receipt_name_for_part(name) for name in checkpoint.completed_parts},
            is_generated_receipt_name,
        ),
    )
    return tuple(
        f"{directory.name}/{path.name}"
        for directory, committed, recognized in groups
        for path in _generated_artifacts(directory, recognized)
        if path.name not in committed
    )


def _generated_artifacts(directory: Path, recognized: Callable[[str], bool]) -> tuple[Path, ...]:
    """List one artifact directory, refusing anything a worker never writes."""
    if not directory.exists():
        return ()
    children = tuple(sorted(directory.iterdir()))
    for child in children:
        _require_generated_artifact(child, recognized)
    return children


def _require_generated_artifact(path: Path, recognized: Callable[[str], bool]) -> None:
    if path.is_symlink() or not path.is_file():
        raise GridOperatorError(f"shard state contains an unusable artifact: {path.name}")
    if not recognized(path.name):
        raise GridOperatorError(f"shard state contains an unknown artifact: {path.name}")


def _quarantine_artifact(state: ShardPaths, relative: str) -> None:
    destination = state.root / QUARANTINE_DIRNAME / relative
    if destination.exists() or destination.is_symlink():
        raise GridOperatorError(f"quarantine already holds an artifact: {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(state.root / relative, destination)
    fsync_directory(destination.parent)


def _verify_immutable_prepared_artifacts(
    paths: JobPaths, config: dict[str, object], script_bytes: bytes
) -> None:
    _verify_immutable_config(paths.config, config)
    _verify_immutable_script(paths.script, script_bytes)


def _verify_immutable_config(path: Path, expected: dict[str, object]) -> None:
    _require_immutable_file(path, "prepared job config is immutable and must be present")
    if _read_json(path, "job config") != expected:
        raise GridOperatorError("prepared job config is immutable; restage with a new job bundle")


def _verify_immutable_script(path: Path, expected: bytes) -> None:
    _require_immutable_file(path, "prepared job config is immutable and its script must be present")
    if path.read_bytes() != expected:
        raise GridOperatorError("prepared job config is immutable; restage with a new job bundle")


def _require_immutable_file(path: Path, message: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise GridOperatorError(message)


def _job_config_payload(
    bundle: JobBundle, *, processing_seconds: int, batch_size: int, walltime_seconds: int
) -> dict[str, object]:
    # ``prepare_job`` renders the job script from these same three values first, and
    # ``render_job_script`` validates them; checking them again here would only repeat
    # that call with the same arguments.
    return {
        "job_config_schema_version": JOB_CONFIG_SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "processing_seconds": processing_seconds,
        "batch_size": batch_size,
        "walltime_seconds": walltime_seconds,
        "cores": REQUIRED_CORES,
        "thread_limit": 1,
    }


def _read_job_config(path: Path, bundle: JobBundle) -> dict[str, int]:
    reader = require_object(
        _read_json(path, "job config"), error=GridOperatorError, label="job config"
    )
    if reader.integer("job_config_schema_version") != JOB_CONFIG_SCHEMA_VERSION:
        raise GridOperatorError("unsupported job config schema version")
    if reader.text("bundle_id") != bundle.bundle_id:
        raise GridOperatorError("job config bundle id does not match the prepared bundle")
    values = {
        "processing_seconds": reader.integer("processing_seconds"),
        "batch_size": reader.integer("batch_size"),
        "walltime_seconds": reader.integer("walltime_seconds"),
        "cores": reader.integer("cores"),
        "thread_limit": reader.integer("thread_limit"),
    }
    _validate_job_limits(
        values["processing_seconds"], values["batch_size"], values["walltime_seconds"]
    )
    if values["cores"] != REQUIRED_CORES:
        raise GridOperatorError(f"job config must request exactly {REQUIRED_CORES} core")
    if values["thread_limit"] != 1:
        raise GridOperatorError("job config thread limit must be 1")
    return values


def _existing_bundle(paths: JobPaths) -> JobBundle | None:
    if paths.bundle.is_symlink():
        raise GridOperatorError(f"prepared bundle must not be a symlink: {paths.bundle}")
    if not paths.bundle.is_file():
        return None
    return read_bundle(paths.bundle)


def read_bundle(path: Path) -> JobBundle:
    """Read and validate one prepared bundle."""
    return JobBundle.from_payload(_read_json(path, "bundle"))


def read_intent(path: Path) -> SubmissionIntent:
    """Read and validate one durable submission intent."""
    return SubmissionIntent.from_payload(_read_json(path, "intent"))


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # pragma: no mutate - codec alias only
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GridOperatorError(f"cannot read {label} {path}: {error}") from error
