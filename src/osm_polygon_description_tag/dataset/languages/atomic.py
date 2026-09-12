"""Crash-safe atomic writes for language-run state.

Every writer fsyncs the temporary file's contents, atomically renames it over
the destination, and only then fsyncs the containing directory, so a crash at
any point leaves either the previous content or the complete new content.
"""

import os
import uuid
from collections.abc import Callable
from pathlib import Path

from osm_polygon_description_tag.dataset.manifest import _fsync_dir
from osm_polygon_description_tag.runtime.serialization import canonical_json_bytes


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably replace ``path`` with ``content``."""
    atomic_write_via(path, lambda temp: temp.write_bytes(content))


def atomic_write_json(path: Path, payload: object) -> None:
    """Durably replace ``path`` with the canonical JSON encoding of ``payload``."""
    atomic_write_bytes(path, canonical_json_bytes(payload))


def atomic_write_via(path: Path, produce: Callable[[Path], object]) -> None:
    """Durably replace ``path`` with content ``produce`` writes to a temp path.

    The callable receives a temporary path inside the destination directory and
    must write the complete artifact to it; a partially written temporary file
    is removed instead of being promoted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        produce(temp)
        _fsync_file(temp)
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


__all__ = [
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_via",
    "canonical_json_bytes",
]
