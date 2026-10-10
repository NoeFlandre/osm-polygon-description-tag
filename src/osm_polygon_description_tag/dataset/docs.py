"""Deterministic dataset-card and derived-media generation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from osm_polygon_description_tag.dataset.canonical_rows import (
    CANONICAL_ROW_POLICY_SHA256,
    CANONICAL_ROW_POLICY_VERSION,
)
from osm_polygon_description_tag.dataset.geography import (
    DEFAULT_H3_RESOLUTION,
    aggregate_h3_density,
    render_area_histogram,
)
from osm_polygon_description_tag.dataset.geography.area_histogram import (
    AREA_HISTOGRAM_RENDER_VERSION,
    aggregate_area_histogram,
    area_histogram_input_sha256,
)
from osm_polygon_description_tag.dataset.geography.basemap import bundled_basemap_path
from osm_polygon_description_tag.dataset.geography.card import (
    H3_MAP_ASSET_RELATIVE_PATH,
    H3_MAP_END_MARKER,
    H3_MAP_START_MARKER,
    H3_MAP_TITLE,
    insert_map_block,
    install_map_block,
    normalize_map_prose,
)
from osm_polygon_description_tag.dataset.geography.rendering import render_density_map
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.stats import collect_stats
from osm_polygon_description_tag.dataset.stats_card import (
    AREA_HISTOGRAM_ASSET_RELATIVE_PATH,
    render_stats_block,
)
from osm_polygon_description_tag.dataset.stats_manifest import ReportingError
from osm_polygon_description_tag.dataset.text import TEXT_CONTRACT_VERSION
from osm_polygon_description_tag.runtime.atomic import atomic_write_bytes
from osm_polygon_description_tag.runtime.resources import dataset_card_hero
from osm_polygon_description_tag.runtime.text_io import read_text_utf8, utf8_bytes

_H3_MAP_CACHE_SCHEMA_VERSION = 2
_H3_MAP_RENDER_VERSION = 2
_DATASET_CARD_HERO_FILENAME = "dataset-card-hero.png"
_DATASET_CARD_HERO_ASSET_RELATIVE_PATH = f"assets/{_DATASET_CARD_HERO_FILENAME}"
_STATS_START_MARKER = "<!-- GENERATED:STATS:START -->"
_STATS_END_MARKER = "<!-- GENERATED:STATS:END -->"
_GENERATED_PATTERN = re.compile(
    rf"({_STATS_START_MARKER}\r?\n)(.*?)({_STATS_END_MARKER})", re.DOTALL
)
_FRONT_MATTER_OPEN = re.compile(r"\A---[ \t]*(?P<newline>\r?\n)")
_FRONT_MATTER_CLOSE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read cache metadata, treating invalid data as absent."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _h3_map_input_sha256(stats: Mapping[str, Any]) -> str:
    """Return the stable identity of every input that can affect the map."""
    file_inputs = [
        {"parquet": str(entry["parquet"]), "output_sha256": str(entry["output_sha256"])}
        for entry in stats.get("files", [])
        if isinstance(entry, Mapping)
    ]
    file_inputs.sort(key=lambda entry: entry["parquet"])
    payload = {
        "cache_schema_version": _H3_MAP_CACHE_SCHEMA_VERSION,
        "render_version": _H3_MAP_RENDER_VERSION,
        "h3_resolution": DEFAULT_H3_RESOLUTION,
        "canonical_row_policy_version": CANONICAL_ROW_POLICY_VERSION,
        "canonical_row_policy_sha256": CANONICAL_ROW_POLICY_SHA256,
        "text_contract_version": TEXT_CONTRACT_VERSION,
        "basemap_sha256": file_sha256(bundled_basemap_path()),
        "files": file_inputs,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(utf8_bytes(encoded)).hexdigest()


def _atomic_write_if_changed(path: Path, data: bytes) -> bool:
    """Atomically write bytes only when the destination bytes differ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return False
    atomic_write_bytes(path, data)
    return True


def _render_h3_map_block() -> str:
    """Render the dataset-card map body."""
    return f"![{H3_MAP_TITLE}]({H3_MAP_ASSET_RELATIVE_PATH})\n"


def _area_histogram_input_sha256(stats: Mapping[str, Any]) -> str:
    """Return the stable area-histogram cache identity."""
    mapping = {
        str(entry["parquet"]): str(entry["output_sha256"]) for entry in stats.get("files", [])
    }
    return area_histogram_input_sha256(mapping)


def _cached_h3_occupied_cells(
    map_path: Path,
    previous_stats: Mapping[str, Any],
    input_sha256: str,
) -> int | None:
    occupied = previous_stats.get("h3_occupied_cells")
    if not map_path.is_file() or previous_stats.get("h3_map_input_sha256") != input_sha256:
        return None
    if not _is_nonnegative_int(occupied):
        return None
    return occupied


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ensure_h3_map(
    data_root: Path,
    stats: Mapping[str, Any],
    previous_stats: Mapping[str, Any],
) -> tuple[str, int]:
    input_sha256 = _h3_map_input_sha256(stats)
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    occupied_cells = _cached_h3_occupied_cells(map_path, previous_stats, input_sha256)
    if occupied_cells is None:
        h3_counts = aggregate_h3_density(data_root)
        occupied_cells = len(h3_counts)
        render_density_map(h3_counts, map_path)
    return input_sha256, occupied_cells


def _histogram_cache_is_valid(
    histogram_path: Path,
    previous_stats: Mapping[str, Any],
    input_sha256: str,
) -> bool:
    total_rows = previous_stats.get("area_histogram_total_rows")
    if not histogram_path.is_file():
        return False
    if previous_stats.get("area_histogram_input_sha256") != input_sha256:
        return False
    if previous_stats.get("area_histogram_render_version") != AREA_HISTOGRAM_RENDER_VERSION:
        return False
    return _is_nonnegative_int(total_rows)


def _ensure_area_histogram(
    data_root: Path,
    stats: Mapping[str, Any],
    previous_stats: Mapping[str, Any],
) -> tuple[str, int]:
    input_sha256 = _area_histogram_input_sha256(stats)
    histogram_path = data_root / AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    if not _histogram_cache_is_valid(histogram_path, previous_stats, input_sha256):
        render_area_histogram(
            aggregate_area_histogram(data_root, require_successful_text=False),
            histogram_path,
        )
    return input_sha256, int(stats["rows"])


def _write_dataset_hero(data_root: Path) -> None:
    _atomic_write_if_changed(
        data_root / _DATASET_CARD_HERO_ASSET_RELATIVE_PATH,
        dataset_card_hero().read_bytes(),
    )


def _card_source(
    data_root: Path,
    template_path: Path,
    *,
    preserve_existing: bool,
) -> tuple[str, bool]:
    """Return the card source and whether it is the already-published card.

    Normal generation starts from the supplied template so template updates
    take effect. A release explicitly opts into the existing card so remote
    language annotations and other published prose remain byte-for-byte intact.
    """
    existing_path = data_root / "README.md"
    if preserve_existing and existing_path.is_file():
        return read_text_utf8(existing_path), True
    return read_text_utf8(template_path), False


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _generated_block_separator(readme: str, newline: str) -> str:
    if not readme:
        return ""
    return newline if readme.endswith(newline) else newline * 2


def _append_generated_block(readme: str, block: str, newline: str) -> str:
    """Append a generated block with the card's existing separator style."""
    return readme + _generated_block_separator(readme, newline) + block


def _front_matter_end(readme: str) -> int | None:
    opening = _FRONT_MATTER_OPEN.match(readme)
    if opening is None:
        return None
    closing = _FRONT_MATTER_CLOSE.search(readme, opening.end())
    if closing is None:
        raise ReportingError("existing README has unterminated YAML front matter")

    return closing.end()


def _front_matter_separator(readme: str, position: int, newline: str) -> str:
    if readme[position - 1 : position] in ("\n", "\r"):
        return ""
    return newline


def _inserted_block_terminator(remainder: str, newline: str) -> str:
    """Return the newline that keeps an inserted block off the following line.

    Body text that starts immediately after the front matter carries no leading
    newline of its own, so without this the block and the first body line would
    be concatenated into one line.
    """
    if not remainder or remainder[:1] in ("\n", "\r"):
        return ""
    return newline


def _insert_after_front_matter(readme: str, block: str, newline: str) -> str | None:
    """Insert a generated block immediately after complete YAML front matter."""
    position = _front_matter_end(readme)
    if position is None:
        return None

    remainder = readme[position:]
    leading = _front_matter_separator(readme, position, newline)
    trailing = _inserted_block_terminator(remainder, newline)
    return readme[:position] + leading + block + trailing + remainder


def _insert_stats_block(readme: str, block: str, newline: str) -> str:
    """Insert a new stats block without changing any existing card bytes."""
    inserted = _insert_after_front_matter(readme, block, newline)
    return inserted if inserted is not None else _append_generated_block(readme, block, newline)


def _stats_marker_count(readme: str) -> int:
    starts = readme.count(_STATS_START_MARKER)
    ends = readme.count(_STATS_END_MARKER)
    if starts != ends or starts > 1:
        raise ReportingError("existing README has malformed generated stats markers")
    return starts


def _replace_stats_block(readme: str, block: str) -> str:
    if _GENERATED_PATTERN.search(readme) is None:
        raise ReportingError("existing README has malformed generated stats markers")
    # _stats_marker_count refuses more than one block before this runs, so
    # replacing "the first" and "all" are the same substitution here.
    replace_one = 1  # pragma: no mutate
    # pragma: no mutate start - one block only, so "first" and "all" agree
    return _GENERATED_PATTERN.sub(
        lambda match: match.group(1) + block + match.group(3), readme, count=replace_one
    )
    # pragma: no mutate end


def _update_stats_block(readme: str, stats: dict[str, Any], stats_sha256: str) -> str:
    """Replace the generated stats block, or insert one into a card without it."""
    starts = _stats_marker_count(readme)
    newline = _newline_for(readme)
    block = render_stats_block(stats, stats_sha256).replace("\n", newline)
    if starts == 0:
        marker_block = f"{_STATS_START_MARKER}{newline}{block}{_STATS_END_MARKER}{newline}"
        return _insert_stats_block(readme, marker_block, newline)
    return _replace_stats_block(readme, block)


def card_has_stats_block(card: str) -> bool:
    """Report whether a dataset card already carries the generated stats block."""
    return _GENERATED_PATTERN.search(card) is not None


def _map_marker_counts(readme: str) -> tuple[int, int]:
    return readme.count(H3_MAP_START_MARKER), readme.count(H3_MAP_END_MARKER)


def _update_map_block(readme: str, block_body: str) -> str:
    """Insert or refresh the map block while leaving malformed cards untouched."""
    h3_starts, h3_ends = _map_marker_counts(readme)
    if h3_starts == 0 and h3_ends == 0:
        updated = insert_map_block(readme, block_body)
    elif h3_starts == 1 and h3_ends == 1:
        updated = install_map_block(readme, block_body)
    else:
        return readme
    return normalize_map_prose(updated)


def _write_dataset_docs(
    data_root: Path,
    template_path: Path,
    stats: dict[str, Any],
    *,
    preserve_existing: bool = False,
) -> None:
    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stats_sha256 = hashlib.sha256(utf8_bytes(stats_json)).hexdigest()
    source, is_published_card = _card_source(
        data_root, template_path, preserve_existing=preserve_existing
    )
    if not is_published_card and not card_has_stats_block(source):
        raise ReportingError(f"template missing GENERATED:STATS markers: {template_path}")
    readme = _update_stats_block(source, stats, stats_sha256)
    readme = _update_map_block(readme, _render_h3_map_block())
    stats_bytes = utf8_bytes(stats_json)
    readme_bytes = utf8_bytes(readme)
    _atomic_write_if_changed(data_root / "stats.json", stats_bytes)
    _atomic_write_if_changed(data_root / "README.md", readme_bytes)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    preserve_existing: bool = False,
) -> dict[str, Any]:
    """Write deterministic stats, README, and derived media artifacts."""
    stats = collect_stats(data_root)
    previous_stats = _read_json_object(data_root / "stats.json")
    map_input_sha256, occupied_cells = _ensure_h3_map(data_root, stats, previous_stats)
    stats["h3_map_input_sha256"] = map_input_sha256
    stats["h3_occupied_cells"] = occupied_cells

    histogram_input_sha256, histogram_total_rows = _ensure_area_histogram(
        data_root, stats, previous_stats
    )
    stats["area_histogram_input_sha256"] = histogram_input_sha256
    stats["area_histogram_render_version"] = AREA_HISTOGRAM_RENDER_VERSION
    stats["area_histogram_total_rows"] = histogram_total_rows
    _write_dataset_hero(data_root)
    if preserve_existing:
        _write_dataset_docs(
            data_root,
            template_path,
            stats,
            preserve_existing=True,
        )
    else:
        _write_dataset_docs(data_root, template_path, stats)
    return stats


__all__ = ["card_has_stats_block", "generate_dataset_docs"]
