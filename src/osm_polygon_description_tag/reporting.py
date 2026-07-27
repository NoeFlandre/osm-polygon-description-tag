"""Artifact-derived statistics and deterministic dataset-card generation.

Reporting reads only validated Parquet files with matching manifests, computes
factual aggregate statistics, and regenerates the marked section of the dataset
card. Handwritten numeric claims inside generated sections are prohibited.
"""

import hashlib
import json
import math
import os
import re
import statistics
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq

from osm_polygon_description_tag.manifest import (
    ManifestError,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.schema import SCHEMA_VERSION

_STATS_SCHEMA_VERSION = 1
_STATS_COLUMNS = [
    "osm_type",
    "geometry_type",
    "area_m2",
    "timestamp",
    "description",
    "localized_descriptions",
]
_GENERATED_PATTERN = re.compile(
    r"(<!-- GENERATED:STATS:START -->\n)(.*?)(<!-- GENERATED:STATS:END -->)", re.DOTALL
)


class ReportingError(ValueError):
    """Raised when artifacts/manifests are missing, stale, or inconsistent."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = percentile / 100.0 * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[int(rank)]
    fraction = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def collect_stats(data_root: Path, *, clock: Callable[[], str] = utc_now_iso) -> dict[str, Any]:
    """Aggregate factual statistics from validated artifacts and matching manifests."""
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise ReportingError(f"missing data/ or manifests/ under {data_root}")

    parquets = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    parquet_stems = {path.name.removesuffix(".parquet") for path in parquets}
    manifest_stems = {
        path.name.removesuffix(".manifest.json") for path in manifests_dir.glob("*.manifest.json")
    }
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise ReportingError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")

    osm_types: dict[str, int] = {}
    geometry_types: dict[str, int] = {}
    suffixes: dict[str, int] = {}
    areas: list[float] = []
    rejections: dict[str, int] = {}
    rows = 0
    base_rows = 0
    localized_rows = 0
    emitted_features = 0
    source_bytes = 0
    output_bytes = 0
    min_ts: datetime | None = None
    max_ts: datetime | None = None

    for parquet in parquets:
        stem = parquet.name.removesuffix(".parquet")
        try:
            manifest = read_manifest(manifests_dir / f"{stem}.manifest.json")
        except ManifestError as error:
            raise ReportingError(f"cannot read manifest for {stem}: {error}") from error
        actual_output = output_identity_for(parquet)
        if manifest.output != actual_output:
            raise ReportingError(f"stale output identity for {parquet.name}")
        output_bytes += parquet.stat().st_size
        source_bytes += manifest.source.size_bytes
        emitted_features += manifest.counts.emitted_features
        for reason, count in manifest.counts.rejections.items():
            rejections[reason] = rejections.get(reason, 0) + count

        parquet_file = pq.ParquetFile(parquet)
        for batch in parquet_file.iter_batches(columns=_STATS_COLUMNS):
            osm_type_col = cast(list[str], batch.column("osm_type").to_pylist())
            geometry_col = cast(list[str], batch.column("geometry_type").to_pylist())
            area_col = cast(list[float], batch.column("area_m2").to_pylist())
            timestamp_col = cast("list[datetime | None]", batch.column("timestamp").to_pylist())
            description_col = cast("list[str | None]", batch.column("description").to_pylist())
            localized_col = cast(
                "list[list[tuple[str, str]]]", batch.column("localized_descriptions").to_pylist()
            )
            for index in range(batch.num_rows):
                rows += 1
                osm_key = osm_type_col[index]
                osm_types[osm_key] = osm_types.get(osm_key, 0) + 1
                geom_key = geometry_col[index]
                geometry_types[geom_key] = geometry_types.get(geom_key, 0) + 1
                areas.append(float(area_col[index]))
                if description_col[index] is not None:
                    base_rows += 1
                localized = localized_col[index]
                if localized:
                    localized_rows += 1
                for suffix, _value in localized:
                    suffixes[suffix] = suffixes.get(suffix, 0) + 1
                timestamp = timestamp_col[index]
                if timestamp is not None:
                    if min_ts is None or timestamp < min_ts:
                        min_ts = timestamp
                    if max_ts is None or timestamp > max_ts:
                        max_ts = timestamp

    sorted_areas = sorted(areas)
    return {
        "stats_schema_version": _STATS_SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generation_timestamp_utc": clock(),
        "output_files": len(parquets),
        "rows": rows,
        "emitted_features": emitted_features,
        "osm_types": dict(sorted(osm_types.items())),
        "geometry_types": dict(sorted(geometry_types.items())),
        "description_suffixes": dict(sorted(suffixes.items())),
        "base_description_rows": base_rows,
        "localized_description_rows": localized_rows,
        "rejections": dict(sorted(rejections.items())),
        "source_bytes_total": source_bytes,
        "output_bytes_total": output_bytes,
        "area_m2_count": len(sorted_areas),
        "area_m2_min_m2": sorted_areas[0] if sorted_areas else None,
        "area_m2_p25_m2": _percentile(sorted_areas, 25),
        "area_m2_median_m2": statistics.median(sorted_areas) if sorted_areas else None,
        "area_m2_p75_m2": _percentile(sorted_areas, 75),
        "area_m2_max_m2": sorted_areas[-1] if sorted_areas else None,
        "data_min_timestamp_utc": min_ts.isoformat() if min_ts else None,
        "data_max_timestamp_utc": max_ts.isoformat() if max_ts else None,
    }


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _render_stats_block(stats: dict[str, Any], stats_sha256: str) -> str:
    lines: list[str] = [
        f"<!-- stats_sha256: {stats_sha256} -->",
        f"<!-- stats_schema_version: {stats['stats_schema_version']} -->",
        f"<!-- schema_version: {stats['schema_version']} -->",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Output files | {_fmt_int(stats['output_files'])} |",
        f"| Rows | {_fmt_int(stats['rows'])} |",
        f"| Emitted features | {_fmt_int(stats['emitted_features'])} |",
        f"| Base description rows | {_fmt_int(stats['base_description_rows'])} |",
        f"| Localized description rows | {_fmt_int(stats['localized_description_rows'])} |",
        f"| Source bytes total | {_fmt_int(stats['source_bytes_total'])} |",
        f"| Output bytes total | {_fmt_int(stats['output_bytes_total'])} |",
        "",
    ]

    def _count_table(title: str, counts: dict[str, int]) -> None:
        lines.append(f"**{title}**")
        lines.append("")
        lines.append("| Key | Count |")
        lines.append("| --- | --- |")
        for key, count in counts.items():
            lines.append(f"| {key} | {_fmt_int(count)} |")
        lines.append("")

    _count_table("Counts by OSM type", stats["osm_types"])
    _count_table("Counts by geometry type", stats["geometry_types"])
    _count_table(
        "Description suffix frequencies (exact suffix, not validated as a language code)",
        stats["description_suffixes"],
    )
    _count_table("Transformation rejections by reason", stats["rejections"])

    lines.append("**Area distribution (square metres)**")
    lines.append("")
    lines.append("| Statistic | Value (m²) |")
    lines.append("| --- | --- |")
    for label, key in (
        ("Minimum", "area_m2_min_m2"),
        ("p25", "area_m2_p25_m2"),
        ("Median", "area_m2_median_m2"),
        ("p75", "area_m2_p75_m2"),
        ("Maximum", "area_m2_max_m2"),
    ):
        lines.append(f"| {label} | {stats[key]} |")
    lines.append("")
    lines.append(f"Generated at {stats['generation_timestamp_utc']} (UTC).")
    lines.append("")
    return "\n".join(lines)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Write canonical ``stats.json`` and regenerate the dataset card's stats block."""
    stats = collect_stats(data_root, clock=clock)
    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stats_sha256 = hashlib.sha256(stats_json.encode("utf-8")).hexdigest()
    template = template_path.read_text(encoding="utf-8")
    if not _GENERATED_PATTERN.search(template):
        raise ReportingError(f"template missing GENERATED:STATS markers: {template_path}")
    rendered_block = _render_stats_block(stats, stats_sha256)
    readme = _GENERATED_PATTERN.sub(
        lambda match: match.group(1) + rendered_block + match.group(3), template
    )
    _atomic_write_text(data_root / "stats.json", stats_json)
    _atomic_write_text(data_root / "README.md", readme)
    return stats
