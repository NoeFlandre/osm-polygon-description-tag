"""Durable locks and resumable-state validation shared by Grid operations."""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from osm_polygon_description_tag.dataset.languages.checkpoint import ShardPaths

from .models import GRID_SUBMISSION_LOCK_FILENAME, QUARANTINE_DIRNAME, GridOperatorError


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


def fsync_directory(path: Path) -> None:
    """Flush a directory entry after atomically replacing owned state."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise GridOperatorError(f"cannot fsync directory {path}: {error}") from error


# Keep the old spelling as an alias, rather than a wrapper, so monkeypatching
# either compatibility name can be restored without creating a recursive alias.
_fsync_directory = fsync_directory


def validate_resume_root(state: ShardPaths) -> None:
    if state.root.is_symlink():
        raise GridOperatorError("resume state directory must not be a symlink")
    if state.root.exists() and not state.root.is_dir():
        raise GridOperatorError("resume state path must be a directory")


def resume_checkpoint_is_present(state: ShardPaths) -> bool:
    from .bundle import _shard_checkpoint

    if _shard_checkpoint(state) is not None:
        return True
    if resume_root_has_children(state):
        raise GridOperatorError("resume artifacts exist without a checkpoint")
    return False


def resume_root_has_children(state: ShardPaths) -> bool:
    return state.root.exists() and any(state.root.iterdir())


def validate_resume_layout(state: ShardPaths) -> None:
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
