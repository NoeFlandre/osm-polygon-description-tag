"""Crash-safe, byte-stable PNG writes shared by geography renderers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from osm_polygon_description_tag.runtime.atomic import atomic_write_via

PNG_METADATA_SOFTWARE: Final[str] = "osm-polygon-description-tag"


class _IdenticalOutputError(Exception):
    """Raised inside the producer when the render matches the existing file."""


def atomic_save_png(fig: Any, output_path: Path) -> None:
    """Save ``fig`` to ``output_path`` through a durable atomic replacement.

    The temporary file is created beside the destination, fsynced before
    replacement, and removed on every code path. Byte-identical output keeps
    the existing file so deterministic reruns preserve its mtime and inode.
    """

    def render(temp_path: Path) -> None:
        fig.savefig(
            str(temp_path),
            format="png",
            facecolor="white",
            metadata={"Software": PNG_METADATA_SOFTWARE},
        )
        if output_path.exists() and output_path.read_bytes() == temp_path.read_bytes():
            raise _IdenticalOutputError

    try:
        atomic_write_via(output_path, render)
    except _IdenticalOutputError:
        return


__all__ = ["PNG_METADATA_SOFTWARE", "atomic_save_png"]
