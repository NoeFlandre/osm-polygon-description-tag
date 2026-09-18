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
H3_MAP_TITLE: Final[str] = (
    "H3 density of canonical globally unique `(osm_type, osm_id)` polygons "
    "with successfully extracted trimmed non-empty description text"
)
H3_MAP_OVERLAP_CAPTION: Final[str] = (
    "Regional overlap duplicates were removed globally; this is not a count of regional rows."
)
H3_MAP_DESCRIPTION: Final[str] = (
    "Hexbin density of every canonical globally unique `(osm_type, osm_id)` polygon\n"
    "with successfully extracted trimmed non-empty description text at H3 resolution\n"
    "3, drawn from each canonical row's geometry centroid on a logarithmic scale.\n"
    "Regional overlap duplicates are removed globally; this is not a count of\n"
    "regional rows. Lighter cells contain more polygons."
)

# These are the two map paragraphs shipped by earlier card revisions. They
# are recognized exactly so a release can correct stale contract wording while
# leaving all unrelated handwritten card content untouched.
_LEGACY_MAP_DESCRIPTIONS: Final[tuple[str, ...]] = (
    "Hexbin density of every canonical globally unique `(osm_type, osm_id)` polygon\n"
    "with successfully extracted trimmed non-empty description text at H3 resolution\n"
    "3, drawn from each row's geometry centroid on a logarithmic scale. Lighter\n"
    "cells contain more polygons.",
    "Hexbin density of every described polygon at H3 resolution 3, drawn from each\n"
    "row's geometry centroid on a logarithmic scale. Lighter cells contain more\n"
    "polygons.",
)

_MARKER_PATTERN = re.compile(
    rf"({re.escape(H3_MAP_START_MARKER)}\r?\n).*?"
    rf"({re.escape(H3_MAP_END_MARKER)}\r?\n)",
    re.DOTALL,
)


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_block_body(block_body: str, newline: str) -> str:
    return block_body.replace("\r\n", "\n").replace("\n", newline).rstrip("\r\n")


def _insert_new_map_block(template: str, block_body: str) -> str:
    """Build and place a new map block in a card without map markers."""
    newline = _newline_for(template)
    normalized_body = _normalize_block_body(block_body, newline)
    block = f"{H3_MAP_START_MARKER}{newline}{normalized_body}{newline}{H3_MAP_END_MARKER}{newline}"
    stats_match = re.search(rf"{re.escape('<!-- GENERATED:STATS:START -->')}\r?\n", template)
    if stats_match is not None:
        return template[: stats_match.start()] + block + template[stats_match.start() :]
    separator = "" if not template else (newline if template.endswith(newline) else newline * 2)
    return template + separator + block


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


def normalize_map_prose(text: str) -> str:
    """Update only known legacy map prose to the current contract wording."""
    newline = _newline_for(text)
    # pragma: no mutate start - this fixed prose contains no sentinel text
    canonical = H3_MAP_DESCRIPTION.replace("\n", newline)
    # pragma: no mutate end
    for legacy in _LEGACY_MAP_DESCRIPTIONS:
        # pragma: no mutate start - the fixed legacy paragraphs contain no sentinel text
        legacy_with_newline = legacy.replace("\n", newline)
        # pragma: no mutate end
        if legacy_with_newline in text:
            return text.replace(legacy_with_newline, canonical, 1)
    return text


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
    newline = "\r\n" if match.group(1).endswith("\r\n") else "\n"
    normalized_body = _normalize_block_body(block_body, newline)
    replacement = f"{match.group(1)}{normalized_body}{newline}{match.group(2)}"
    return template[: match.start()] + replacement + template[match.end() :]


def insert_map_block(template: str, block_body: str) -> str:
    """Add the map marker block when an older card has no map markers.

    Existing marker pairs are refreshed through :func:`install_map_block`.
    When no pair exists, the new block is inserted immediately before the
    stats block, or appended when the card has no stats marker.
    """
    start_count = template.count(H3_MAP_START_MARKER)
    end_count = template.count(H3_MAP_END_MARKER)
    if start_count == 1 and end_count == 1:
        return install_map_block(template, block_body)
    if start_count != 0 or end_count != 0:
        raise ValueError("dataset card has malformed H3 map markers")
    return _insert_new_map_block(template, block_body)


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
    _validate_marker_counts(start_count, end_count)
    if start_count == 1 and end_count == 1:
        return
    new_text = _template_with_map_markers(text, asset_relative_path)
    _atomic_write_template(template_path, new_text)


def _validate_marker_counts(start_count: int, end_count: int) -> None:
    if start_count > 1 or end_count > 1:
        raise ValueError(
            f"dataset card template must contain a unique H3 map marker block; "
            f"found {start_count} start markers and {end_count} end markers"
        )


def _template_with_map_markers(text: str, asset_relative_path: str) -> str:
    # Insert the marker block immediately before the stats marker block so the
    # map image appears near the top of the dataset card. The H3 marker block
    # ends with the same newline that introduces the stats marker, so the
    # surrounding prose is preserved byte-for-byte.
    newline = _newline_for(text)
    stats_match = re.search(rf"{re.escape('<!-- GENERATED:STATS:START -->')}\r?\n", text)
    if stats_match is None:
        raise ValueError("template missing GENERATED:STATS:START marker; cannot insert map block")
    block = (
        f"{H3_MAP_START_MARKER}{newline}"
        f"![{H3_MAP_TITLE}]({asset_relative_path}){newline}"
        f"{H3_MAP_END_MARKER}{newline}"
    )
    return text[: stats_match.start()] + block + text[stats_match.start() :]


def _atomic_write_template(template_path: Path, new_text: str) -> None:
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
    "H3_MAP_DESCRIPTION",
    "H3_MAP_END_MARKER",
    "H3_MAP_OVERLAP_CAPTION",
    "H3_MAP_START_MARKER",
    "H3_MAP_TITLE",
    "insert_map_block",
    "install_map_block",
    "normalize_map_prose",
    "render_map_block",
    "write_map_block_marker_to_template",
]
