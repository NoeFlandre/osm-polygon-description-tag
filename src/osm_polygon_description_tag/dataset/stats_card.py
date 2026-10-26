"""Statistics block and section renderers for the generated dataset card."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from osm_polygon_description_tag.dataset.numeric import coerce_float_values as _coerce_float_values
from osm_polygon_description_tag.dataset.stats import TEXT_REJECTION_REASONS

AREA_HISTOGRAM_FILENAME = "area_distribution.png"
AREA_HISTOGRAM_ASSET_RELATIVE_PATH = f"assets/{AREA_HISTOGRAM_FILENAME}"
AREA_HISTOGRAM_TITLE = "Area distribution of description-tagged polygons"
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


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
    lines.extend(
        f"| `{reason}` | {_fmt_int(int(text_rejections.get(reason, 0)))} |"
        for reason in TEXT_REJECTION_REASONS
    )
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
    # The isinstance guard below normalises anything that is not a Mapping,
    # so the default here cannot reach the caller: {} and None are the same.
    geometry_types = stats.get("geometry_types", {})  # pragma: no mutate
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


def render_stats_block(stats: dict[str, Any], stats_sha256: str) -> str:
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
            f"![{AREA_HISTOGRAM_TITLE}]({AREA_HISTOGRAM_ASSET_RELATIVE_PATH})",
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


__all__ = ["AREA_HISTOGRAM_ASSET_RELATIVE_PATH", "render_stats_block"]
