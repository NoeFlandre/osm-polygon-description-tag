"""Portable relative-path validation shared by snapshots and checkpoints.

Both the immutable input snapshot and the durable shard state address files by
a relative path that must survive being moved between machines. A single
validator keeps the two from drifting apart, and keeps traversal and absolute
paths out of every persisted payload.
"""

from pathlib import Path


def relative_posix_path(value: object, *, error: type[Exception], label: str) -> str:
    """Return ``value`` as a portable relative POSIX path.

    Raises ``error`` when the value is empty, absolute, Windows-spelled, or
    contains a traversal component.
    """
    path = _as_path(value, error, label)
    _reject_absolute(path, error, label)
    _reject_traversal(path, error, label)
    return path.as_posix()


def _as_path(value: object, error: type[Exception], label: str) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    raise error(f"{label} must be a non-empty relative path")


def _reject_absolute(path: Path, error: type[Exception], label: str) -> None:
    if path.is_absolute() or "\\" in path.as_posix():
        raise error(f"{label} must be relative and portable")


def _reject_traversal(path: Path, error: type[Exception], label: str) -> None:
    if not path.parts or ".." in path.parts:
        raise error(f"{label} must not contain traversal")


__all__ = ["relative_posix_path"]
