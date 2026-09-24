"""On-disk job state: locks, shard checkpoints, orphan quarantine, and resume checks."""

import errno
import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

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
from osm_polygon_description_tag.dataset.languages.validation import (
    RunReport,
    validate_run,
)
from osm_polygon_description_tag.runtime.atomic import (
    fsync_dir,
)
from osm_polygon_description_tag.workflow.grid_job_script import (
    _validate_batch_size,
)
from osm_polygon_description_tag.workflow.grid_models import (
    GRID_SUBMISSION_LOCK_FILENAME,
    QUARANTINE_DIRNAME,
    GridOperatorError,
    JobBundle,
)


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
    _validate_resume_root(state)
    existing = _shard_checkpoint(state)
    if existing is not None:
        _require_bound_checkpoint(existing, bundle)
        return existing
    if _resume_root_has_children(state):
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
    _validate_resume_root(state)
    if not _resume_checkpoint_is_present(state):
        return ()
    checkpoint = read_checkpoint(state.checkpoint)
    _validate_resume_layout(state)
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
    _fsync_directory(destination.parent)


def _validate_resume_state(run_dir: Path, shard: str) -> None:
    state = shard_paths(run_dir, shard)
    _validate_resume_root(state)
    if not _resume_checkpoint_is_present(state):
        return
    _validate_resume_layout(state)
    _validate_resume_report(collect_results(run_dir, shard))


def _validate_resume_root(state: ShardPaths) -> None:
    if state.root.is_symlink():
        raise GridOperatorError("resume state directory must not be a symlink")
    if state.root.exists() and not state.root.is_dir():
        raise GridOperatorError("resume state path must be a directory")


def _resume_checkpoint_is_present(state: ShardPaths) -> bool:
    if _shard_checkpoint(state) is not None:
        return True
    if _resume_root_has_children(state):
        raise GridOperatorError("resume artifacts exist without a checkpoint")
    return False


def _resume_root_has_children(state: ShardPaths) -> bool:
    return state.root.exists() and any(state.root.iterdir())


def _validate_resume_layout(state: ShardPaths) -> None:
    allowed = {
        state.checkpoint.name,
        state.parts.name,
        state.receipts.name,
        QUARANTINE_DIRNAME,
    }
    for child in state.root.iterdir():
        if child.name not in allowed:
            raise GridOperatorError(f"resume state contains an unexpected artifact: {child.name}")
        if child.name != state.checkpoint.name:
            _require_resume_directory(child)


def _require_resume_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise GridOperatorError(f"resume state directory is invalid: {path}")


def _validate_resume_report(report: RunReport) -> None:
    if report.issues:
        raise GridOperatorError("resume state failed collection validation")
    selected = report.shards[0]
    if selected.issues:
        raise GridOperatorError("resume state failed collection validation")


def _fsync_directory(path: Path) -> None:
    try:
        fsync_dir(path)
    except OSError as error:
        raise GridOperatorError(f"cannot fsync directory {path}: {error}") from error


@contextmanager
def submission_lock(run_dir: Path) -> Iterator[None]:
    """Hold the run-wide non-blocking lock around submission state changes."""
    _require_run_directory(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / GRID_SUBMISSION_LOCK_FILENAME
    _require_lock_path(lock_path)
    handle = _open_lock(lock_path)
    try:
        _acquire_lock(handle, run_dir)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _require_run_directory(run_dir: Path) -> None:
    if run_dir.is_symlink():
        raise GridOperatorError(f"run directory is not a regular directory: {run_dir}")
    if run_dir.exists() and not run_dir.is_dir():
        raise GridOperatorError(f"run directory is not a regular directory: {run_dir}")


def _require_lock_path(lock_path: Path) -> None:
    if lock_path.is_symlink():
        raise GridOperatorError(f"submission lock must be a regular file: {lock_path}")
    if lock_path.exists() and not lock_path.is_file():
        raise GridOperatorError(f"submission lock must be a regular file: {lock_path}")


def _open_lock(lock_path: Path) -> TextIO:
    try:
        return lock_path.open("a+")
    except OSError as error:
        raise GridOperatorError(f"cannot open submission lock {lock_path}: {error}") from error


def _acquire_lock(handle: TextIO, run_dir: Path) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise GridOperatorError(f"submission lock is already held: {run_dir}") from error
        raise GridOperatorError(f"cannot acquire submission lock {run_dir}: {error}") from error


def collect_results(run_dir: Path, shard: str) -> RunReport:
    """Validate what a completed or paused job actually produced."""
    return validate_run(run_dir, shards=(shard,))
