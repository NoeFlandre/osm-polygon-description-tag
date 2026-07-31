"""Dataset-card integration for the H3 density map.

This module owns the exact marker block that is rewritten in the dataset
card and the helper that performs the byte-stable substitution. The
map block is wrapped in ``<!-- GENERATED:H3_MAP:START/END -->`` markers
so a regeneration changes only that block; the surrounding handwritten
prose is preserved byte-for-byte.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Final

H3_MAP_START_MARKER: Final[str] = "<!-- GENERATED:H3_MAP:START -->"
H3_MAP_END_MARKER: Final[str] = "<!-- GENERATED:H3_MAP:END -->"
H3_MAP_ASSET_RELATIVE_PATH: Final[str] = "assets/description_polygon_density.png"
H3_MAP_TITLE: Final[str] = "H3 density of description-tagged polygons"

_MARKER_PATTERN = re.compile(
    rf"({re.escape(H3_MAP_START_MARKER)}\n).*?({re.escape(H3_MAP_END_MARKER)}\n)",
    re.DOTALL,
)


def render_map_block() -> str:
    """Return the canonical map block to install in the dataset card.

    The image reference is relative to the Hugging Face dataset
    repository root, so the same README.md renders correctly on the
    Hub without rewriting paths.
    """
    return (
        f"{H3_MAP_START_MARKER}\n"
        f"![{H3_MAP_TITLE}]({H3_MAP_ASSET_RELATIVE_PATH})\n"
        f"{H3_MAP_END_MARKER}\n"
    )


def install_map_block(template: str, block_body: str) -> str:
    """Replace the map marker block in ``template`` with ``block_body``.

    The replacement is a pure function of the template, the markers, and
    the new body. Outside the marker block, the template is preserved
    byte-for-byte.

    Raises :class:`ValueError` when the template does not contain the
    marker block or contains it more than once.
    """
    matches = list(_MARKER_PATTERN.finditer(template))
    if not matches:
        raise ValueError(
            f"dataset card template missing H3 map markers: "
            f"expected exactly one {H3_MAP_START_MARKER}...{H3_MAP_END_MARKER} block"
        )
    if len(matches) > 1:
        raise ValueError(
            f"dataset card template must contain a unique H3 map marker block; found {len(matches)}"
        )
    match = matches[0]
    replacement = f"{match.group(1)}{block_body}{match.group(2)}"
    return template[: match.start()] + replacement + template[match.end() :]


def write_map_block_marker_to_template(
    template_path: Path, *, asset_relative_path: str = H3_MAP_ASSET_RELATIVE_PATH
) -> None:
    """Inject the H3 map marker block into ``template_path`` if missing.

    This helper is used at template install time so the packaged
    template always carries the marker block. It writes atomically and
    is a no-op when the markers are already present.
    """
    text = template_path.read_text(encoding="utf-8")
    start_count = text.count(H3_MAP_START_MARKER)
    end_count = text.count(H3_MAP_END_MARKER)
    if start_count > 1 or end_count > 1:
        raise ValueError(
            f"dataset card template must contain a unique H3 map marker block; "
            f"found {start_count} start markers and {end_count} end markers"
        )
    if start_count == 1 and end_count == 1:
        return
    # Insert the marker block immediately before the stats marker block so the
    # map image appears near the top of the dataset card. The H3 marker block
    # ends with the same newline that introduces the stats marker, so the
    # surrounding prose is preserved byte-for-byte.
    stats_start = "<!-- GENERATED:STATS:START -->\n"
    if stats_start not in text:
        raise ValueError(f"template missing {stats_start!r} marker; cannot insert map block")
    block = (
        f"{H3_MAP_START_MARKER}\n![{H3_MAP_TITLE}]({asset_relative_path})\n{H3_MAP_END_MARKER}\n"
    )
    new_text = text.replace(stats_start, block + stats_start, 1)
    tmp = template_path.with_name(f".{template_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, template_path)
    finally:
        if tmp.exists():
            tmp.unlink()


__all__ = [
    "H3_MAP_ASSET_RELATIVE_PATH",
    "H3_MAP_END_MARKER",
    "H3_MAP_START_MARKER",
    "H3_MAP_TITLE",
    "install_map_block",
    "render_map_block",
    "write_map_block_marker_to_template",
]
