"""Crash-safe, byte-stable PNG writes shared by geography renderers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Final

PNG_METADATA_SOFTWARE: Final[str] = "osm-polygon-description-tag"


def atomic_save_png(fig: Any, output_path: Path) -> None:
    """Save ``fig`` to ``output_path`` through a durable atomic replacement.

    The temporary file is created beside the destination, fsynced before
    replacement, and removed on every code path. Byte-identical output keeps
    the existing file so deterministic reruns preserve its mtime and inode.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        fig.savefig(
            str(tmp_path),
            format="png",
            facecolor="white",
            metadata={"Software": PNG_METADATA_SOFTWARE},
        )
        with open(tmp_path, "rb") as handle:
            os.fsync(handle.fileno())
        if output_path.exists() and output_path.read_bytes() == tmp_path.read_bytes():
            tmp_path.unlink()
            return
        os.replace(tmp_path, output_path)
        directory_fd = os.open(str(output_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


__all__ = ["PNG_METADATA_SOFTWARE", "atomic_save_png"]
