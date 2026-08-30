"""Deterministic dataset-card and derived-media generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

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
    install_map_block,
)
from osm_polygon_description_tag.dataset.geography.rendering import render_density_map
from osm_polygon_description_tag.dataset.manifest import file_sha256
from osm_polygon_description_tag.dataset.stats import ReportingError, collect_stats, utc_now_iso
from osm_polygon_description_tag.runtime.resources import dataset_card_hero

_H3_MAP_CACHE_SCHEMA_VERSION = 1
_H3_MAP_RENDER_VERSION = 2
_AREA_HISTOGRAM_FILENAME = "area_distribution.png"
_AREA_HISTOGRAM_ASSET_RELATIVE_PATH = f"assets/{_AREA_HISTOGRAM_FILENAME}"
_AREA_HISTOGRAM_TITLE = "Area distribution of description-tagged polygons"
_DATASET_CARD_HERO_FILENAME = "dataset-card-hero.png"
_DATASET_CARD_HERO_ASSET_RELATIVE_PATH = f"assets/{_DATASET_CARD_HERO_FILENAME}"
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
_GENERATED_PATTERN = re.compile(
    r"(<!-- GENERATED:STATS:START -->\n)(.*?)(<!-- GENERATED:STATS:END -->)", re.DOTALL
)


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
        "basemap_sha256": file_sha256(bundled_basemap_path()),
        "files": file_inputs,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    # pragma: no mutate end


def _atomic_write_if_changed(path: Path, data: bytes) -> bool:
    """Atomically write bytes only when the destination bytes differ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return False
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return True
    finally:
        if temp.exists():
            temp.unlink()


def _write_if_changed(path: Path, text: str) -> bool:
    """Atomically write UTF-8 text only when the destination bytes differ."""
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    return _atomic_write_if_changed(path, text.encode("utf-8"))
    # pragma: no mutate end


def _write_bytes_if_changed(path: Path, data: bytes) -> bool:
    """Atomically write binary data only when bytes differ."""
    return _atomic_write_if_changed(path, data)


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _scale_bytes(value: int) -> tuple[float, int]:
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(_BYTE_UNITS) - 1:
        size /= 1024
        unit_index += 1
    return size, unit_index


def _fmt_bytes(value: int) -> str:
    size, unit_index = _scale_bytes(value)
    decimals = 0 if unit_index == 0 else 1
    return f"{size:,.{decimals}f} {_BYTE_UNITS[unit_index]}"


def _fmt_median(value: float | None) -> str:
    if value is None:
        return "—"
    return _fmt_int(int(value)) if value.is_integer() else f"{value:,.1f}"


def _render_stats_block(stats: dict[str, Any], stats_sha256: str) -> str:
    lines: list[str] = [
        f"<!-- stats_sha256: {stats_sha256} -->",
        f"<!-- stats_schema_version: {stats['stats_schema_version']} -->",
        f"<!-- schema_version: {stats['schema_version']} -->",
        "",
        "## Dataset at a glance",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Polygons | {_fmt_int(stats['rows'])} |",
        f"| Parquet files | {_fmt_int(stats['output_files'])} |",
        f"| Download size | {_fmt_bytes(stats['output_bytes_total'])} |",
        f"| Duplicate rows removed | {_fmt_int(stats['deduplicated_rows'])} |",
        f"| Closed ways | {_fmt_int(stats['osm_types'].get('way', 0))} |",
        f"| Relations | {_fmt_int(stats['osm_types'].get('relation', 0))} |",
        f"| Polygon geometries | {_fmt_int(stats['geometry_types'].get('Polygon', 0))} |",
        f"| MultiPolygon geometries | {_fmt_int(stats['geometry_types'].get('MultiPolygon', 0))} |",
        "",
        "## Description coverage",
        "",
        "| Description type | Values | Total words | Median words per description |",
        "| --- | ---: | ---: | ---: |",
        "| Base descriptions | "
        f"{_fmt_int(stats['base_description_values'])} | "
        f"{_fmt_int(stats['base_description_words_total'])} | "
        f"{_fmt_median(stats['base_description_words_median'])} |",
        "| Localized descriptions | "
        f"{_fmt_int(stats['localized_description_values'])} | "
        f"{_fmt_int(stats['localized_description_words_total'])} | "
        f"{_fmt_median(stats['localized_description_words_median'])} |",
        "",
    ]
    top_suffixes = sorted(
        stats["description_suffixes"].items(), key=lambda item: (-item[1], item[0])
    )[:10]
    if top_suffixes:
        lines.extend(
            [
                "### Most common localized suffixes",
                "",
                "These are exact OSM tag suffixes and are not validated language codes.",
                "",
                "| Suffix | Description values |",
                "| --- | ---: |",
            ]
        )
        lines.extend(f"| `{suffix}` | {_fmt_int(count)} |" for suffix, count in top_suffixes)
        lines.append("")
    lines.extend(
        [
            "### Area distribution",
            "",
            f"![{_AREA_HISTOGRAM_TITLE}]({_AREA_HISTOGRAM_ASSET_RELATIVE_PATH})",
            "",
            "Area buckets span <1 m² to >=100B m² on a logarithmic scale; "
            "each bar shows the number of polygons in that bucket "
            f"(total {_fmt_int(stats['rows'])}).",
            "",
        ]
    )
    if stats["data_min_timestamp_utc"] and stats["data_max_timestamp_utc"]:
        lines.extend(
            [
                "**OSM object timestamps (UTC):** "
                f"{stats['data_min_timestamp_utc']} to {stats['data_max_timestamp_utc']}",
                "",
            ]
        )
    lines.extend(
        [
            "Detailed machine-readable statistics, exact suffix frequencies, rejection counts, "
            "and per-file SHA-256 provenance are available in [`stats.json`](stats.json).",
            "",
        ]
    )
    return "\n".join(lines)


def _render_h3_map_block() -> str:
    """Render the dataset-card map body."""
    return f"![{H3_MAP_TITLE}]({H3_MAP_ASSET_RELATIVE_PATH})\n"


def _write_h3_map_png(
    data_root: Path,
    total_rows: int,
    occupied_cells: int,
    *,
    counts: Mapping[str, int] | None = None,
) -> None:
    """Render the H3 density PNG, accepting precomputed counts for tests."""
    render_density_map(
        counts if counts is not None else aggregate_h3_density(data_root),
        data_root / H3_MAP_ASSET_RELATIVE_PATH,
    )


def _area_histogram_input_sha256(stats: Mapping[str, Any]) -> str:
    """Return the stable area-histogram cache identity."""
    mapping = {
        str(entry["parquet"]): str(entry["output_sha256"]) for entry in stats.get("files", [])
    }
    return area_histogram_input_sha256(mapping)


def _write_area_histogram_png(
    data_root: Path,
    *,
    counts: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Aggregate and render the area histogram."""
    values = counts if counts is not None else aggregate_area_histogram(data_root)
    render_area_histogram(values, data_root / _AREA_HISTOGRAM_ASSET_RELATIVE_PATH)
    return dict(values)


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
    total_rows: int,
    stats: Mapping[str, Any],
    previous_stats: Mapping[str, Any],
) -> tuple[str, int]:
    input_sha256 = _h3_map_input_sha256(stats)
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    occupied_cells = _cached_h3_occupied_cells(map_path, previous_stats, input_sha256)
    if occupied_cells is None:
        h3_counts = aggregate_h3_density(data_root)
        occupied_cells = len(h3_counts)
        _write_h3_map_png(data_root, total_rows, occupied_cells, counts=h3_counts)
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
    histogram_path = data_root / _AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    if not _histogram_cache_is_valid(histogram_path, previous_stats, input_sha256):
        _write_area_histogram_png(data_root)
    return input_sha256, int(stats["rows"])


def _write_dataset_hero(data_root: Path) -> None:
    _write_bytes_if_changed(
        data_root / _DATASET_CARD_HERO_ASSET_RELATIVE_PATH,
        dataset_card_hero().read_bytes(),
    )


def _write_dataset_docs(
    data_root: Path,
    template_path: Path,
    stats: dict[str, Any],
) -> None:
    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    stats_sha256 = hashlib.sha256(stats_json.encode("utf-8")).hexdigest()
    # pragma: no mutate end
    # pragma: no mutate start - UTF-8 read aliases are runtime-equivalent
    template = template_path.read_text(encoding="utf-8")
    # pragma: no mutate end
    if not _GENERATED_PATTERN.search(template):
        raise ReportingError(f"template missing GENERATED:STATS markers: {template_path}")
    readme = _GENERATED_PATTERN.sub(
        lambda match: match.group(1) + _render_stats_block(stats, stats_sha256) + match.group(3),
        template,
    )
    if H3_MAP_START_MARKER in readme and H3_MAP_END_MARKER in readme:
        readme = install_map_block(
            readme,
            _render_h3_map_block(),
        )
    _write_if_changed(data_root / "stats.json", stats_json)
    _write_if_changed(data_root / "README.md", readme)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Write deterministic stats, README, and derived media artifacts."""
    stats = collect_stats(data_root, clock=clock)
    total_rows = int(stats["rows"])
    previous_stats = _read_json_object(data_root / "stats.json")
    map_input_sha256, occupied_cells = _ensure_h3_map(data_root, total_rows, stats, previous_stats)
    stats["h3_map_input_sha256"] = map_input_sha256
    stats["h3_occupied_cells"] = occupied_cells

    histogram_input_sha256, histogram_total_rows = _ensure_area_histogram(
        data_root, stats, previous_stats
    )
    stats["area_histogram_input_sha256"] = histogram_input_sha256
    stats["area_histogram_render_version"] = AREA_HISTOGRAM_RENDER_VERSION
    stats["area_histogram_total_rows"] = histogram_total_rows
    _write_dataset_hero(data_root)
    _write_dataset_docs(data_root, template_path, stats)
    return stats


__all__ = ["generate_dataset_docs"]
