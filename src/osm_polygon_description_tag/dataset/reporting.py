"""Artifact-derived statistics and deterministic dataset-card generation.

Reporting reads only validated Parquet files with matching manifests, computes
factual aggregate statistics, and regenerates the marked section of the dataset
card. Handwritten numeric claims inside generated sections are prohibited.

Bounded memory: aggregate statistics are computed by streaming each Parquet
file into an in-memory DuckDB instance using ``read_parquet`` and per-batch
``ArrowTable`` ingestion. Exact area quantiles use ``quantile_cont`` on the
DuckDB-backed ``area_m2`` column, so peak memory is bounded regardless of the
total row count.

Amendment 2: ``stats.json`` and the regenerated README are pure functions
of the validated Parquets, the matching manifests, and the card template.
Wall-clock values are not serialized. Atomic, write-if-changed writes
preserve mtimes when bytes are byte-identical.

H3 aggregation contract: every call to :func:`generate_dataset_docs` performs
at most one H3 aggregation pass. A deterministic input identity derived from
the finalized Parquet hashes, renderer revision, H3 resolution, and bundled
basemap lets README-only regeneration reuse the existing PNG without reading
the Parquets again for map counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow.parquet as pq

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
from osm_polygon_description_tag.dataset.manifest import (
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA_VERSION
from osm_polygon_description_tag.runtime.resources import dataset_card_hero

_STATS_SCHEMA_VERSION = 5
_H3_MAP_CACHE_SCHEMA_VERSION = 1
_H3_MAP_RENDER_VERSION = 2
_AREA_HISTOGRAM_FILENAME = "area_distribution.png"
_AREA_HISTOGRAM_ASSET_RELATIVE_PATH = f"assets/{_AREA_HISTOGRAM_FILENAME}"
_AREA_HISTOGRAM_TITLE = "Area distribution of description-tagged polygons"
_DATASET_CARD_HERO_FILENAME = "dataset-card-hero.png"
_DATASET_CARD_HERO_ASSET_RELATIVE_PATH = f"assets/{_DATASET_CARD_HERO_FILENAME}"
_QUANTILE_PROBABILITIES = [0.25, 0.5, 0.75]
_GENERATED_PATTERN = re.compile(
    r"(<!-- GENERATED:STATS:START -->\n)(.*?)(<!-- GENERATED:STATS:END -->)", re.DOTALL
)
_FEATURE_COLUMNS = [
    "osm_type",
    "osm_id",
    "geometry_type",
    "area_m2",
    "timestamp",
    "name",
    "localized_names",
    "description",
    "localized_descriptions",
]


class ReportingError(ValueError):
    """Raised when artifacts/manifests are missing, stale, or inconsistent."""


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object for cache metadata, treating invalid data as absent."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _h3_map_input_sha256(stats: Mapping[str, Any]) -> str:
    """Return the stable identity of every input that can affect the map."""
    files = stats.get("files", [])
    file_inputs = [
        {
            "parquet": str(entry["parquet"]),
            "output_sha256": str(entry["output_sha256"]),
        }
        for entry in files
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
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _write_if_changed(path: Path, text: str) -> bool:
    """Atomically write ``text`` to ``path`` only when bytes differ.

    Returns True if a write occurred, False when the file was already
    byte-identical. Identical regeneration preserves mtime.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        os.replace(temp, path)
        return True
    finally:
        if temp.exists():
            temp.unlink()


def _write_bytes_if_changed(path: Path, data: bytes) -> bool:
    """Atomically write ``data`` to ``path`` only when bytes differ.

    Mirrors :func:`_write_if_changed` for binary asset files so identical
    regeneration preserves mtime and avoids touching the underlying
    directory entry. Returns ``True`` when a write occurred.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return False
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        with open(temp, "rb") as handle:
            os.fsync(handle.fileno())
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        os.replace(temp, path)
        return True
    finally:
        if temp.exists():
            temp.unlink()


def _new_connection(data_root: Path) -> duckdb.DuckDBPyConnection:
    work_root = data_root / ".work" / "duckdb"
    work_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    connection.execute("SET temp_directory = ?", [str(work_root)])
    return connection


def _quantile_or_none(
    connection: duckdb.DuckDBPyConnection,
    column: str,
    probability: float,
) -> float | None:
    if column != "area_m2":
        return None
    query = (
        f"SELECT quantile_cont({column}, {probability}) FROM features WHERE {column} IS NOT NULL"  # noqa: S608
    )
    result = connection.execute(query).fetchone()
    if result is None or result[0] is None:
        return None
    return float(result[0])


def _safe_map(value: object) -> dict[str, int]:
    """Convert DuckDB map output into a deterministic string-to-int dict."""
    if value is None:
        return {}
    items = value.items() if isinstance(value, dict) else []
    cleaned: dict[str, int] = {}
    for key, count in items:
        if key is None:
            continue
        cleaned[str(key)] = int(cast(int, count))
    return cleaned


def _suffix_counts(connection: duckdb.DuckDBPyConnection, map_column: str) -> dict[str, int]:
    if rows := connection.execute(
        f"""
        SELECT entry.key AS suffix, COUNT(*) AS value FROM (
            SELECT unnest(map_entries({map_column})) AS entry FROM features
            WHERE cardinality({map_column}) > 0
        ) GROUP BY entry.key ORDER BY entry.key
        """  # noqa: S608 - map_column is allowlisted by the caller
    ).fetchall():
        return {key: int(value) for key, value in rows}
    return {}


def _description_word_stats(
    connection: duckdb.DuckDBPyConnection,
    *,
    localized: bool,
) -> tuple[int, int, float | None]:
    values_query = (
        """
        SELECT entry.value AS value
        FROM (
            SELECT unnest(map_entries(localized_descriptions)) AS entry
            FROM features
            WHERE cardinality(localized_descriptions) > 0
        )
        """
        if localized
        else "SELECT description AS value FROM features WHERE description IS NOT NULL"
    )
    row = connection.execute(
        rf"""
        WITH description_values AS ({values_query}),
        word_counts AS (
            SELECT list_count(regexp_extract_all(value, '[^\s\p{{Z}}]+')) AS word_count
            FROM description_values
        )
        SELECT COUNT(*), COALESCE(SUM(word_count), 0), quantile_cont(word_count, 0.5)
        FROM word_counts
        """  # noqa: S608 - values_query is selected from two static statements
    ).fetchone()
    if row is None:
        return 0, 0, None
    return int(row[0]), int(row[1]), float(row[2]) if row[2] is not None else None


def collect_stats(
    data_root: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
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

    # Validate manifest identities first to fail fast and free of partial state.
    for parquet in parquets:
        stem = parquet.name.removesuffix(".parquet")
        try:
            manifest = read_manifest(manifests_dir / f"{stem}.manifest.json")
        except ManifestError as error:
            raise ReportingError(f"cannot read manifest for {stem}: {error}") from error
        actual_output = output_identity_for(parquet)
        if manifest.output != actual_output:
            raise ReportingError(f"stale output identity for {parquet.name}")

    connection = _new_connection(data_root)
    try:
        connection.execute(
            """
            CREATE TABLE features (
                osm_type VARCHAR NOT NULL,
                osm_id BIGINT NOT NULL,
                geometry_type VARCHAR NOT NULL,
                area_m2 DOUBLE NOT NULL,
                timestamp TIMESTAMP,
                name VARCHAR,
                localized_names MAP(VARCHAR, VARCHAR) NOT NULL,
                description VARCHAR,
                localized_descriptions MAP(VARCHAR, VARCHAR) NOT NULL,
                source VARCHAR NOT NULL
            )
            """
        )

        # Streaming batch ingestion keeps per-PBF memory bounded.
        for parquet in parquets:
            file_reader = pq.ParquetFile(parquet)
            for batch in file_reader.iter_batches(
                columns=_FEATURE_COLUMNS,
                batch_size=4096,
            ):
                connection.register("batch", batch)
                connection.execute(
                    """
                    INSERT INTO features
                    SELECT
                        osm_type,
                        osm_id,
                        geometry_type,
                        area_m2,
                        timestamp,
                        name,
                        CASE WHEN localized_names IS NULL THEN MAP() ELSE localized_names END,
                        description,
                        CASE WHEN localized_descriptions IS NULL
                             THEN MAP() ELSE localized_descriptions END,
                        ? AS source
                    FROM batch
                    """,
                    [parquet.name],
                )
                connection.unregister("batch")

        rows_result = connection.execute("SELECT COUNT(*) FROM features").fetchone()
        rows = int(rows_result[0] if rows_result else 0)
        unique_result = connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM features)"
        ).fetchone()
        unique_osm_objects = int(unique_result[0] if unique_result else 0)
        if rows == 0:
            osm_types: dict[str, int] = {}
            geometry_types: dict[str, int] = {}
            description_suffixes: dict[str, int] = {}
            name_suffixes: dict[str, int] = {}
            base_description_rows = 0
            localized_description_rows = 0
            base_name_rows = 0
            localized_name_rows = 0
            area_min: float | None = None
            area_max: float | None = None
            area_p25: float | None = None
            area_median: float | None = None
            area_p75: float | None = None
            min_ts: str | None = None
            max_ts: str | None = None
        else:
            osm_types = dict(
                (key, int(value))
                for key, value in connection.execute(
                    "SELECT osm_type, COUNT(*) FROM features GROUP BY osm_type ORDER BY osm_type"
                ).fetchall()
            )
            geometry_types = dict(
                (key, int(value))
                for key, value in connection.execute(
                    "SELECT geometry_type, COUNT(*) FROM features GROUP BY geometry_type "
                    "ORDER BY geometry_type"
                ).fetchall()
            )
            description_suffixes = _suffix_counts(connection, "localized_descriptions")
            name_suffixes = _suffix_counts(connection, "localized_names")
            base_count = connection.execute(
                "SELECT COUNT(*) FROM features WHERE description IS NOT NULL"
            ).fetchone()
            base_description_rows = int(base_count[0] if base_count else 0)
            localized_count = connection.execute(
                "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0"
            ).fetchone()
            localized_description_rows = int(localized_count[0] if localized_count else 0)
            base_name_count = connection.execute(
                "SELECT COUNT(*) FROM features WHERE name IS NOT NULL"
            ).fetchone()
            base_name_rows = int(base_name_count[0] if base_name_count else 0)
            localized_name_count = connection.execute(
                "SELECT COUNT(*) FROM features WHERE cardinality(localized_names) > 0"
            ).fetchone()
            localized_name_rows = int(localized_name_count[0] if localized_name_count else 0)
            area_min = _quantile_or_none(connection, "area_m2", 0.0)
            area_max = _quantile_or_none(connection, "area_m2", 1.0)
            area_p25 = _quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[0])
            area_median = _quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[1])
            area_p75 = _quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[2])
            ts_row = connection.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM features WHERE timestamp IS NOT NULL"
            ).fetchone()
            min_ts = ts_row[0].isoformat() if ts_row and ts_row[0] else None
            max_ts = ts_row[1].isoformat() if ts_row and ts_row[1] else None
        (
            base_description_values,
            base_description_words_total,
            base_description_words_median,
        ) = _description_word_stats(connection, localized=False)
        (
            localized_description_values,
            localized_description_words_total,
            localized_description_words_median,
        ) = _description_word_stats(connection, localized=True)
    finally:
        connection.close()

    rejections: dict[str, int] = {}
    emitted_features = 0
    source_bytes = 0
    output_bytes = 0
    file_records: list[dict[str, Any]] = []
    for parquet in parquets:
        stem = parquet.name.removesuffix(".parquet")
        manifest = read_manifest(manifests_dir / f"{stem}.manifest.json")
        output_bytes += parquet.stat().st_size
        source_bytes += manifest.source.size_bytes
        emitted_features += manifest.counts.emitted_features
        for reason, count in manifest.counts.rejections.items():
            rejections[reason] = rejections.get(reason, 0) + count
        rows_in_file = int(
            pq.ParquetFile(parquet).metadata.num_rows if pq.ParquetFile(parquet).metadata else 0
        )
        file_records.append(
            {
                "source_pbf": manifest.source.name,
                "parquet": parquet.name,
                "rows": rows_in_file,
                "source_bytes": manifest.source.size_bytes,
                "output_bytes": parquet.stat().st_size,
                "emitted_features": manifest.counts.emitted_features,
                "rejections": dict(sorted(manifest.counts.rejections.items())),
                "source_sha256": manifest.source.sha256,
                "output_sha256": file_sha256(parquet),
            }
        )
    file_records.sort(key=lambda record: record["parquet"])

    return {
        "stats_schema_version": _STATS_SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "output_files": len(parquets),
        "rows": rows,
        "unique_osm_objects": unique_osm_objects,
        "regional_overlap_duplicate_rows": rows - unique_osm_objects,
        "regional_overlap_duplicate_rate": ((rows - unique_osm_objects) / rows if rows else 0.0),
        "emitted_features": emitted_features,
        "osm_types": osm_types,
        "geometry_types": geometry_types,
        "description_suffixes": description_suffixes,
        "name_suffixes": name_suffixes,
        "base_description_rows": base_description_rows,
        "localized_description_rows": localized_description_rows,
        "base_description_values": base_description_values,
        "base_description_words_total": base_description_words_total,
        "base_description_words_median": base_description_words_median,
        "localized_description_values": localized_description_values,
        "localized_description_words_total": localized_description_words_total,
        "localized_description_words_median": localized_description_words_median,
        "base_name_rows": base_name_rows,
        "localized_name_rows": localized_name_rows,
        "rejections": dict(sorted(rejections.items())),
        "source_bytes_total": source_bytes,
        "output_bytes_total": output_bytes,
        "area_m2_count": rows,
        "area_m2_min_m2": area_min,
        "area_m2_p25_m2": area_p25,
        "area_m2_median_m2": area_median,
        "area_m2_p75_m2": area_p75,
        "area_m2_max_m2": area_max,
        "data_min_timestamp_utc": min_ts,
        "data_max_timestamp_utc": max_ts,
        "files": file_records,
    }


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _fmt_median(value: float | None) -> str:
    if value is None:
        return "—"
    return _fmt_int(int(value)) if value.is_integer() else f"{value:,.1f}"


def _fmt_area(value: float | None) -> str:
    return "—" if value is None else f"{value:,.1f}"


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
        stats["description_suffixes"].items(),
        key=lambda item: (-item[1], item[0]),
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
            "Area buckets span "
            "<1 m² to >100B m² on a logarithmic scale; "
            f"each bar shows the number of polygons in that bucket "
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
            "Detailed machine-readable statistics, exact suffix frequencies, "
            "rejection counts, and per-file SHA-256 provenance are available in "
            "[`stats.json`](stats.json).",
            "",
        ]
    )
    return "\n".join(lines)


def _render_h3_map_block(data_root: Path, total_rows: int, occupied_cells: int) -> str:
    """Render the dataset-card map body referencing the H3 density PNG.

    Returns only the body of the map block (the image markdown line), not
    the surrounding marker block. :func:`install_map_block` is responsible
    for placing the body between the start and end markers.
    """
    _ = (data_root, total_rows, occupied_cells)
    return f"![{H3_MAP_TITLE}]({H3_MAP_ASSET_RELATIVE_PATH})\n"


def _write_h3_map_png(
    data_root: Path,
    total_rows: int,
    occupied_cells: int,
    *,
    counts: Mapping[str, int] | None = None,
) -> None:
    """Render the H3 density PNG into ``data_root/assets/``.

    Production callers pass nothing for ``counts`` and this helper
    performs the single aggregation pass. Tests may inject a pre-computed
    count mapping via the ``counts`` keyword to avoid re-running the
    aggregation. Pass-through ``total_rows`` and ``occupied_cells`` are
    accepted for backward compatibility with the historical signature;
    the rendered PNG is byte-identical regardless.
    """
    _ = (total_rows, occupied_cells)
    if counts is None:
        counts = aggregate_h3_density(data_root)
    output = data_root / H3_MAP_ASSET_RELATIVE_PATH
    render_density_map(counts, output)


def _area_histogram_input_sha256(stats: Mapping[str, Any]) -> str:
    """Stable identity for the area-histogram cache derived from finalized Parquets."""
    files = stats.get("files", [])
    mapping = {str(entry["parquet"]): str(entry["output_sha256"]) for entry in files}
    return area_histogram_input_sha256(mapping)


def _write_area_histogram_png(
    data_root: Path,
    *,
    counts: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Aggregate the area histogram and render it into ``assets/``."""
    if counts is None:
        counts = aggregate_area_histogram(data_root)
    output = data_root / _AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    render_area_histogram(counts, output)
    return dict(counts)


def generate_dataset_docs(
    data_root: Path,
    template_path: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Write canonical ``stats.json`` and regenerate the dataset card's stats block.

    The outputs are pure functions of the validated Parquets, the matching
    manifests, and the card template. Wall-clock values are never serialized.
    Identical regeneration is a no-op for file bytes and mtimes.

    The H3 density map is aggregated only when its deterministic input
    identity is absent or stale. The existing PNG is reused for README-only
    changes and when finalized Parquet bytes are unchanged.

    The area distribution histogram follows the same cache discipline:
    recomputation happens only when finalized Parquet output identities
    change or the renderer version is bumped.
    """
    stats = collect_stats(data_root, clock=clock)
    map_input_sha256 = _h3_map_input_sha256(stats)
    total_rows = int(stats["rows"])
    map_path = data_root / H3_MAP_ASSET_RELATIVE_PATH
    previous_stats = _read_json_object(data_root / "stats.json")
    previous_identity = previous_stats.get("h3_map_input_sha256")
    previous_occupied = previous_stats.get("h3_occupied_cells")
    can_reuse_map = (
        map_path.is_file()
        and previous_identity == map_input_sha256
        and isinstance(previous_occupied, int)
        and not isinstance(previous_occupied, bool)
        and previous_occupied >= 0
    )
    if can_reuse_map:
        occupied_cells = previous_occupied
        h3_counts: Mapping[str, int] | None = None
    else:
        h3_counts = aggregate_h3_density(data_root)
        occupied_cells = len(h3_counts)
        _write_h3_map_png(data_root, total_rows, occupied_cells, counts=h3_counts)

    stats["h3_map_input_sha256"] = map_input_sha256
    stats["h3_occupied_cells"] = occupied_cells

    histogram_path = data_root / _AREA_HISTOGRAM_ASSET_RELATIVE_PATH
    histogram_input_sha256 = _area_histogram_input_sha256(stats)
    previous_histogram_identity = previous_stats.get("area_histogram_input_sha256")
    previous_histogram_total = previous_stats.get("area_histogram_total_rows")
    can_reuse_histogram = (
        histogram_path.is_file()
        and previous_histogram_identity == histogram_input_sha256
        and isinstance(previous_histogram_total, int)
        and not isinstance(previous_histogram_total, bool)
        and previous_histogram_total >= 0
    )
    if not can_reuse_histogram:
        _write_area_histogram_png(data_root)

    stats["area_histogram_input_sha256"] = histogram_input_sha256
    stats["area_histogram_render_version"] = AREA_HISTOGRAM_RENDER_VERSION
    stats["area_histogram_total_rows"] = total_rows

    hero_bytes = dataset_card_hero().read_bytes()
    _write_bytes_if_changed(data_root / _DATASET_CARD_HERO_ASSET_RELATIVE_PATH, hero_bytes)

    stats_json = json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stats_sha256 = hashlib.sha256(stats_json.encode("utf-8")).hexdigest()
    template = template_path.read_text(encoding="utf-8")
    if not _GENERATED_PATTERN.search(template):
        raise ReportingError(f"template missing GENERATED:STATS markers: {template_path}")
    rendered_block = _render_stats_block(stats, stats_sha256)
    readme = _GENERATED_PATTERN.sub(
        lambda match: match.group(1) + rendered_block + match.group(3), template
    )

    map_body = _render_h3_map_block(data_root, total_rows, occupied_cells)
    if H3_MAP_START_MARKER in readme and H3_MAP_END_MARKER in readme:
        readme = install_map_block(readme, map_body)

    _write_if_changed(data_root / "stats.json", stats_json)
    _write_if_changed(data_root / "README.md", readme)
    # Backwards-compatible return: callers that consume the stats
    # payload directly continue to work, while the new wrapper exposes
    # additional generation outputs.
    return stats


__all__ = [
    "ReportingError",
    "collect_stats",
    "generate_dataset_docs",
    "utc_now_iso",
]
