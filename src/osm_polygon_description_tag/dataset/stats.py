"""Artifact-derived dataset statistics.

Statistics read only validated Parquet files with matching manifests and are
computed from the finalized artifacts. Handwritten numeric claims are never
introduced here.

Bounded memory: aggregate statistics are computed by streaming each Parquet
file into an in-memory DuckDB instance using ``read_parquet`` and per-batch
``ArrowTable`` ingestion. Exact area quantiles use ``quantile_cont`` on the
DuckDB-backed ``area_m2`` column, so peak memory is bounded regardless of the
total row count.

"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    ManifestError,
    file_sha256,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.schema import SCHEMA_VERSION
from osm_polygon_description_tag.runtime.time import utc_now_iso

STATS_SCHEMA_VERSION = 6
_QUANTILE_PROBABILITIES = [0.25, 0.5, 0.75]
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


def _map_sql_expression(batch: pa.RecordBatch, column: str) -> str:
    """Normalize legacy Arrow maps and Hub-compatible key/value lists in SQL."""
    field = batch.schema.field(column)
    if pa.types.is_map(field.type):
        return column
    if pa.types.is_list(field.type):
        return f"map_from_entries({column})"
    raise ReportingError(f"unsupported mapping representation for {column}: {field.type}")


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
                localized_names_sql = _map_sql_expression(batch, "localized_names")
                localized_descriptions_sql = _map_sql_expression(batch, "localized_descriptions")
                connection.register("batch", batch)
                connection.execute(
                    f"""
                    INSERT INTO features
                    SELECT
                        osm_type,
                        osm_id,
                        geometry_type,
                        area_m2,
                        timestamp,
                        name,
                        CASE WHEN {localized_names_sql} IS NULL
                             THEN MAP() ELSE {localized_names_sql} END,
                        description,
                        CASE WHEN {localized_descriptions_sql} IS NULL
                             THEN MAP() ELSE {localized_descriptions_sql} END,
                        ? AS source
                    FROM batch
                    """,  # noqa: S608 - expressions are internal fixed column names
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
        "stats_schema_version": STATS_SCHEMA_VERSION,
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
        "deduplicated_rows": rejections.get("duplicate_osm_object", 0),
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


__all__ = [
    "STATS_SCHEMA_VERSION",
    "ReportingError",
    "collect_stats",
    "utc_now_iso",
]
