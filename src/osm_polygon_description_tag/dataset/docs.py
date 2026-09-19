"""Deterministic dataset-card and derived-media generation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

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
from osm_polygon_description_tag.dataset.stats import (
    TEXT_REJECTION_REASONS,
    ReportingError,
    collect_stats,
    utc_now_iso,
)
from osm_polygon_description_tag.dataset.text import TEXT_CONTRACT_VERSION
from osm_polygon_description_tag.runtime.resources import dataset_card_hero

_H3_MAP_CACHE_SCHEMA_VERSION = 2
_H3_MAP_RENDER_VERSION = 2
_AREA_HISTOGRAM_FILENAME = "area_distribution.png"
_AREA_HISTOGRAM_ASSET_RELATIVE_PATH = f"assets/{_AREA_HISTOGRAM_FILENAME}"
_AREA_HISTOGRAM_TITLE = "Area distribution of description-tagged polygons"
_DATASET_CARD_HERO_FILENAME = "dataset-card-hero.png"
_DATASET_CARD_HERO_ASSET_RELATIVE_PATH = f"assets/{_DATASET_CARD_HERO_FILENAME}"
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
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


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_bytes(value: int) -> str:
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(_BYTE_UNITS) - 1:
        size /= 1024
        unit_index += 1
    decimals = 0 if unit_index == 0 else 1
    return f"{size:,.{decimals}f} {_BYTE_UNITS[unit_index]}"


def _fmt_median(value: float | None) -> str:
    if value is None:
        return "—"
    return _fmt_int(int(value)) if value.is_integer() else f"{value:,.1f}"


def _fmt_area(value: float | None) -> str:
    """Format an area with deterministic, readable metric units."""
    if value is None or not math.isfinite(value):
        return "—"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f} km²"
    if abs(value) >= 1:
        return f"{value:,.1f} m²"
    return f"{value:.3g} m²"


def _coerce_float_values(values: Sequence[object]) -> tuple[float, ...] | None:
    """Convert object values to floats, returning ``None`` on conversion errors."""
    try:
        return tuple(float(cast(Any, value)) for value in values)  # pragma: no mutate
    except (TypeError, ValueError):
        return None


def _coerce_bbox_coordinates(value: object) -> tuple[float, float, float, float] | None:
    """Convert a persisted ``[min_lon, min_lat, max_lon, max_lat]`` extent."""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    coordinates = _coerce_float_values(value)
    if coordinates is None:
        return None
    if not all(map(math.isfinite, coordinates)):
        return None
    return coordinates[0], coordinates[1], coordinates[2], coordinates[3]


def _fmt_bbox(value: object) -> str:
    """Format a dataset extent for the geometry statistics section."""
    coordinates = _coerce_bbox_coordinates(value)
    if coordinates is None:
        return "—"
    min_x, min_y, max_x, max_y = coordinates
    return f"lon {min_x:.4f}° to {max_x:.4f}°, lat {min_y:.4f}° to {max_y:.4f}°"


def _polygon_count_metrics(stats: Mapping[str, Any]) -> tuple[int, int, int, int]:
    globally_unique = int(stats.get("globally_unique_polygons", stats.get("rows", 0)))
    overlap_duplicates = int(stats.get("regional_overlap_duplicate_rows", 0))
    regional_rows = int(stats.get("regional_rows", globally_unique + overlap_duplicates))
    manifest_duplicates = int(
        stats.get("manifest_duplicate_rows", stats.get("deduplicated_rows", 0))
    )
    return regional_rows, globally_unique, overlap_duplicates, manifest_duplicates


def _successful_text_count(stats: Mapping[str, Any], fallback: int) -> int:
    value = stats.get(
        "unique_polygons_with_successful_nonempty_text",
        stats.get("unique_polygons_with_text", fallback),
    )
    return int(value)


def _render_suffix_section(stats: Mapping[str, Any]) -> list[str]:
    top_suffixes = sorted(
        stats["description_suffixes"].items(), key=lambda item: (-item[1], item[0])
    )[:10]
    if not top_suffixes:
        return []
    lines = [
        "### Most common localized suffixes",
        "",
        "These are exact OSM tag suffixes and are not validated language codes.",
        "",
        "| Suffix | Description values |",
        "| --- | ---: |",
    ]
    lines.extend(f"| `{suffix}` | {_fmt_int(count)} |" for suffix, count in top_suffixes)
    lines.append("")
    return lines


def _render_text_rejection_section(stats: Mapping[str, Any]) -> list[str]:
    text_rejections = stats.get("text_rejection_counts")
    if not isinstance(text_rejections, Mapping):
        text_rejections = {}
    has_persisted_rejection_count = "persisted_text_rejection_rows" in stats
    explanation = (
        "These counts describe rows rejected before publication; the final "
        "polygon and area populations contain only trimmed, non-empty text."
    )
    if has_persisted_rejection_count:
        explanation += (
            " The separate persisted-artifact count covers legacy rows retained "
            "in published Parquet but excluded by the final predicate."
        )
    lines = [
        "### Text-contract exclusions in source manifests",
        "",
        explanation,
        "",
        "| Rejection category | Rows |",
        "| --- | ---: |",
    ]
    for reason in TEXT_REJECTION_REASONS:
        lines.append(f"| `{reason}` | {_fmt_int(int(text_rejections.get(reason, 0)))} |")
    if has_persisted_rejection_count:
        # pragma: no mutate start - key presence makes the fallback unreachable
        lines.extend(
            [
                "",
                "**Persisted artifact rows excluded by the final text predicate:** "
                f"{_fmt_int(int(stats.get('persisted_text_rejection_rows', 0)))}.",
                "",
                "Source/manifest rejection counts and persisted artifact exclusions "
                "are separate populations and are not added together.",
            ]
        )
        # pragma: no mutate end
    lines.append("")
    return lines


def _render_timestamp_section(stats: Mapping[str, Any]) -> list[str]:
    if not stats["data_min_timestamp_utc"] or not stats["data_max_timestamp_utc"]:
        return []
    return [
        "**OSM object timestamps (UTC):** "
        f"{stats['data_min_timestamp_utc']} to {stats['data_max_timestamp_utc']}",
        "",
    ]


def _render_geometry_stats_section(stats: Mapping[str, Any]) -> list[str]:
    """Render the additive geometry statistics section."""
    geometry_types = stats.get("geometry_types", {})
    if not isinstance(geometry_types, Mapping):
        geometry_types = {}
    regional_rows, globally_unique, overlap_duplicates, _manifest_duplicates = (
        _polygon_count_metrics(stats)
    )
    successful_text = _successful_text_count(stats, globally_unique)
    return [
        "## Polygon surface and geometry",
        "",
        "Computed deterministically from the complete published polygon table: "
        f"all {_fmt_int(successful_text)} canonical globally unique "
        "`(osm_type, osm_id)` polygons with successfully extracted trimmed "
        "non-empty description text from "
        f"{_fmt_int(regional_rows)} regional/raw rows across "
        f"{_fmt_int(stats['output_files'])} "
        "Parquet files, using only the dataset's area_m2, bbox, and geometry columns. "
        f"{_fmt_int(overlap_duplicates)} regional-overlap duplicate rows are excluded. "
        "No sampling, truncation, external lookup, or raw-PBF recomputation is used.",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Unique `(osm_type, osm_id)` polygons across regional/raw rows | "
        f"{_fmt_int(globally_unique)} |",
        f"| Canonical globally unique `(osm_type, osm_id)` polygons with "
        f"successfully extracted trimmed non-empty description text | "
        f"{_fmt_int(successful_text)} |",
        "| Surface area (total / mean) | "
        f"{_fmt_area(stats.get('area_m2_total_m2'))} / "
        f"{_fmt_area(stats.get('area_m2_mean_m2'))} |",
        "| Smallest / largest area | "
        f"{_fmt_area(stats.get('area_m2_min_m2'))} / "
        f"{_fmt_area(stats.get('area_m2_max_m2'))} |",
        "| Area p25 / median / p75 | "
        f"{_fmt_area(stats.get('area_m2_p25_m2'))} / "
        f"{_fmt_area(stats.get('area_m2_median_m2'))} / "
        f"{_fmt_area(stats.get('area_m2_p75_m2'))} |",
        f"| Dataset bounding box | {_fmt_bbox(stats.get('dataset_bbox'))} |",
        "| Geometry totals (vertices / rings / holes / MultiPolygon parts) | "
        f"{_fmt_int(stats.get('geometry_vertices_total', 0))} / "
        f"{_fmt_int(stats.get('geometry_rings_total', 0))} / "
        f"{_fmt_int(stats.get('geometry_holes_total', 0))} / "
        f"{_fmt_int(stats.get('multipolygon_components_total', 0))} |",
        "| Polygon / MultiPolygon rows | "
        f"{_fmt_int(geometry_types.get('Polygon', 0))} / "
        f"{_fmt_int(geometry_types.get('MultiPolygon', 0))} |",
        "",
        "The complete machine-readable report is published in stats.json. These values "
        "are generated from the data only and are deterministic for unchanged published "
        "artifacts.",
    ]


def _render_stats_block(stats: dict[str, Any], stats_sha256: str) -> str:
    regional_rows, globally_unique, overlap_duplicates, manifest_duplicates = (
        _polygon_count_metrics(stats)
    )
    successful_text = _successful_text_count(stats, globally_unique)
    lines: list[str] = [
        f"<!-- stats_sha256: {stats_sha256} -->",
        f"<!-- stats_schema_version: {stats['stats_schema_version']} -->",
        f"<!-- schema_version: {stats['schema_version']} -->",
        "",
        "## Dataset at a glance",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Regional/raw polygon rows | {_fmt_int(regional_rows)} |",
        f"| Unique `(osm_type, osm_id)` polygons across regional/raw rows | "
        f"{_fmt_int(globally_unique)} |",
        f"| Canonical globally unique `(osm_type, osm_id)` polygons with "
        f"successfully extracted trimmed non-empty description text | "
        f"{_fmt_int(successful_text)} |",
        f"| Regional-overlap duplicate rows | {_fmt_int(overlap_duplicates)} |",
        f"| Parquet files | {_fmt_int(stats['output_files'])} |",
        f"| Download size | {_fmt_bytes(stats['output_bytes_total'])} |",
        f"| Manifest duplicate rows rejected | {_fmt_int(manifest_duplicates)} |",
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
    lines.extend(_render_suffix_section(stats))
    lines.extend(
        [
            "### Area distribution",
            "",
            f"![{_AREA_HISTOGRAM_TITLE}]({_AREA_HISTOGRAM_ASSET_RELATIVE_PATH})",
            "",
            "Area buckets span <1 m² to >=100B m² on a logarithmic scale; "
            "each bar shows the number of polygons in that bucket "
            f"(total {_fmt_int(successful_text)} canonical globally unique "
            "`(osm_type, osm_id)` polygons with successfully extracted trimmed "
            "non-empty description text).",
            "",
        ]
    )
    lines.extend(_render_text_rejection_section(stats))
    lines.extend(_render_timestamp_section(stats))
    lines.extend(
        [
            "Detailed machine-readable statistics, exact suffix frequencies, rejection counts, "
            "and per-file SHA-256 provenance are available in [`stats.json`](stats.json).",
            "",
        ]
    )
    lines.extend(_render_geometry_stats_section(stats))
    return "\n".join(lines)


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
    histogram_path = data_root / _AREA_HISTOGRAM_ASSET_RELATIVE_PATH
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
        # pragma: no mutate start - UTF-8 read aliases are runtime-equivalent
        return existing_path.read_text(encoding="utf-8"), True
        # pragma: no mutate end
    # pragma: no mutate start - UTF-8 read aliases are runtime-equivalent
    return template_path.read_text(encoding="utf-8"), False
    # pragma: no mutate end


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
    return _GENERATED_PATTERN.sub(
        lambda match: match.group(1) + block + match.group(3), readme, count=1
    )


def _update_stats_block(readme: str, stats: dict[str, Any], stats_sha256: str) -> str:
    """Replace the generated stats block, or insert one into a card without it."""
    starts = _stats_marker_count(readme)
    newline = _newline_for(readme)
    block = _render_stats_block(stats, stats_sha256).replace("\n", newline)
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
    # pragma: no mutate start - UTF-8 codec names are case-insensitive
    stats_sha256 = hashlib.sha256(stats_json.encode("utf-8")).hexdigest()
    # pragma: no mutate end
    source, is_published_card = _card_source(
        data_root, template_path, preserve_existing=preserve_existing
    )
    if not is_published_card and not card_has_stats_block(source):
        raise ReportingError(f"template missing GENERATED:STATS markers: {template_path}")
    readme = _update_stats_block(source, stats, stats_sha256)
    readme = _update_map_block(readme, _render_h3_map_block())
    _write_if_changed(data_root / "stats.json", stats_json)
    _write_if_changed(data_root / "README.md", readme)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
    preserve_existing: bool = False,
) -> dict[str, Any]:
    """Write deterministic stats, README, and derived media artifacts."""
    stats = collect_stats(data_root, clock=clock)
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
