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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    Manifest,
    ManifestError,
    _manifest_path_for,
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


@dataclass(frozen=True)
class _ValidatedArtifact:
    parquet: Path
    manifest: Manifest


@dataclass(frozen=True)
class _FeatureSummary:
    rows: int
    unique_osm_objects: int
    osm_types: dict[str, int]
    geometry_types: dict[str, int]
    description_suffixes: dict[str, int]
    name_suffixes: dict[str, int]
    base_description_rows: int
    localized_description_rows: int
    base_description_values: int
    base_description_words_total: int
    base_description_words_median: float | None
    localized_description_values: int
    localized_description_words_total: int
    localized_description_words_median: float | None
    base_name_rows: int
    localized_name_rows: int
    area_min_m2: float | None
    area_p25_m2: float | None
    area_median_m2: float | None
    area_p75_m2: float | None
    area_max_m2: float | None
    data_min_timestamp_utc: str | None
    data_max_timestamp_utc: str | None


@dataclass(frozen=True)
class _ManifestSummary:
    emitted_features: int
    rejections: dict[str, int]
    source_bytes_total: int
    output_bytes_total: int
    files: list[dict[str, Any]]


def _reporting_directories(data_root: Path) -> tuple[Path, Path]:
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise ReportingError(f"missing data/ or manifests/ under {data_root}")
    return data_dir, manifests_dir


def _matching_parquets(data_dir: Path, manifests_dir: Path) -> list[Path]:
    # pragma: no mutate start - all glob results share data_dir, so Path order equals name order
    parquets = sorted(data_dir.glob("*.parquet"), key=lambda path: path.name)
    # pragma: no mutate end
    parquet_stems = {path.name.removesuffix(".parquet") for path in parquets}
    manifest_stems = {
        path.name.removesuffix(".manifest.json") for path in manifests_dir.glob("*.manifest.json")
    }
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise ReportingError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")
    return parquets


def _validate_artifact(parquet: Path, manifests_dir: Path) -> _ValidatedArtifact:
    stem = parquet.name.removesuffix(".parquet")
    try:
        manifest = read_manifest(_manifest_path_for(parquet.name, manifests_dir.parent))
    except ManifestError as error:
        raise ReportingError(f"cannot read manifest for {stem}: {error}") from error
    actual_output = output_identity_for(parquet)
    if manifest.output != actual_output:
        raise ReportingError(f"stale output identity for {parquet.name}")
    return _ValidatedArtifact(parquet=parquet, manifest=manifest)


def _find_validated_artifacts(data_root: Path) -> tuple[_ValidatedArtifact, ...]:
    data_dir, manifests_dir = _reporting_directories(data_root)
    parquets = _matching_parquets(data_dir, manifests_dir)
    return tuple(_validate_artifact(parquet, manifests_dir) for parquet in parquets)


def _create_feature_table(connection: duckdb.DuckDBPyConnection) -> None:
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


def _insert_batch(
    connection: duckdb.DuckDBPyConnection,
    batch: pa.RecordBatch,
    source_name: str,
) -> None:
    localized_names_sql = _map_sql_expression(batch, "localized_names")
    localized_descriptions_sql = _map_sql_expression(batch, "localized_descriptions")
    connection.register("batch", batch)
    try:
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
            [source_name],
        )
    finally:
        connection.unregister("batch")


def _ingest_features(
    connection: duckdb.DuckDBPyConnection,
    artifacts: tuple[_ValidatedArtifact, ...],
) -> None:
    """Stream every finalized Parquet batch into the feature table."""
    for artifact in artifacts:
        file_reader = pq.ParquetFile(artifact.parquet)
        for batch in file_reader.iter_batches(
            columns=_FEATURE_COLUMNS,
            batch_size=4096,
        ):
            _insert_batch(connection, batch, artifact.parquet.name)


def _query_int(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    result = connection.execute(query).fetchone()
    return int(result[0] if result else 0)


def _ordered_counts(connection: duckdb.DuckDBPyConnection, query: str) -> dict[str, int]:
    return {str(key): int(value) for key, value in connection.execute(query).fetchall()}


def _collect_feature_summary(connection: duckdb.DuckDBPyConnection) -> _FeatureSummary:
    rows = _query_int(connection, "SELECT COUNT(*) FROM features")
    unique_osm_objects = _query_int(
        connection,
        "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM features)",
    )
    osm_types = _ordered_counts(
        connection,
        "SELECT osm_type, COUNT(*) FROM features GROUP BY osm_type ORDER BY osm_type",
    )
    geometry_types = _ordered_counts(
        connection,
        "SELECT geometry_type, COUNT(*) FROM features GROUP BY geometry_type "
        "ORDER BY geometry_type",
    )
    description_suffixes = _suffix_counts(connection, "localized_descriptions")
    name_suffixes = _suffix_counts(connection, "localized_names")
    base_description_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE description IS NOT NULL",
    )
    localized_description_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0",
    )
    base_name_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE name IS NOT NULL",
    )
    localized_name_rows = _query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE cardinality(localized_names) > 0",
    )
    timestamp_row = connection.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM features WHERE timestamp IS NOT NULL"
    ).fetchone()
    min_ts = timestamp_row[0] if timestamp_row else None
    max_ts = timestamp_row[1] if timestamp_row else None
    base_description_values, base_description_words_total, base_description_words_median = (
        _description_word_stats(connection, localized=False)
    )
    (
        localized_description_values,
        localized_description_words_total,
        localized_description_words_median,
    ) = _description_word_stats(connection, localized=True)
    return _FeatureSummary(
        rows=rows,
        unique_osm_objects=unique_osm_objects,
        osm_types=osm_types,
        geometry_types=geometry_types,
        description_suffixes=description_suffixes,
        name_suffixes=name_suffixes,
        base_description_rows=base_description_rows,
        localized_description_rows=localized_description_rows,
        base_description_values=base_description_values,
        base_description_words_total=base_description_words_total,
        base_description_words_median=base_description_words_median,
        localized_description_values=localized_description_values,
        localized_description_words_total=localized_description_words_total,
        localized_description_words_median=localized_description_words_median,
        base_name_rows=base_name_rows,
        localized_name_rows=localized_name_rows,
        area_min_m2=_quantile_or_none(connection, "area_m2", 0.0),
        area_p25_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[0]),
        area_median_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[1]),
        area_p75_m2=_quantile_or_none(connection, "area_m2", _QUANTILE_PROBABILITIES[2]),
        area_max_m2=_quantile_or_none(connection, "area_m2", 1.0),
        data_min_timestamp_utc=min_ts.isoformat() if min_ts else None,
        data_max_timestamp_utc=max_ts.isoformat() if max_ts else None,
    )


def _rows_in_parquet(path: Path) -> int:
    metadata = pq.ParquetFile(path).metadata
    return int(metadata.num_rows if metadata else 0)


def _collect_manifest_summary(
    artifacts: tuple[_ValidatedArtifact, ...],
) -> _ManifestSummary:
    rejections: dict[str, int] = {}
    emitted_features = 0
    source_bytes = 0
    output_bytes = 0
    file_records: list[dict[str, Any]] = []
    for artifact in artifacts:
        parquet = artifact.parquet
        manifest = artifact.manifest
        output_size = parquet.stat().st_size
        output_bytes += output_size
        source_bytes += manifest.source.size_bytes
        emitted_features += manifest.counts.emitted_features
        for reason, count in manifest.counts.rejections.items():
            rejections[reason] = rejections.get(reason, 0) + count
        file_records.append(
            {
                "source_pbf": manifest.source.name,
                "parquet": parquet.name,
                "rows": _rows_in_parquet(parquet),
                "source_bytes": manifest.source.size_bytes,
                "output_bytes": output_size,
                "emitted_features": manifest.counts.emitted_features,
                "rejections": dict(sorted(manifest.counts.rejections.items())),
                "source_sha256": manifest.source.sha256,
                "output_sha256": file_sha256(parquet),
            }
        )
    file_records.sort(key=lambda record: record["parquet"])
    return _ManifestSummary(
        emitted_features=emitted_features,
        rejections=dict(sorted(rejections.items())),
        source_bytes_total=source_bytes,
        output_bytes_total=output_bytes,
        files=file_records,
    )


def _build_stats_payload(
    feature_summary: _FeatureSummary,
    manifest_summary: _ManifestSummary,
) -> dict[str, Any]:
    duplicate_rows = feature_summary.rows - feature_summary.unique_osm_objects
    return {
        "stats_schema_version": STATS_SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "output_files": len(manifest_summary.files),
        "rows": feature_summary.rows,
        "unique_osm_objects": feature_summary.unique_osm_objects,
        "regional_overlap_duplicate_rows": duplicate_rows,
        "regional_overlap_duplicate_rate": (
            duplicate_rows / feature_summary.rows if feature_summary.rows else 0.0
        ),
        "emitted_features": manifest_summary.emitted_features,
        "osm_types": feature_summary.osm_types,
        "geometry_types": feature_summary.geometry_types,
        "description_suffixes": feature_summary.description_suffixes,
        "name_suffixes": feature_summary.name_suffixes,
        "base_description_rows": feature_summary.base_description_rows,
        "localized_description_rows": feature_summary.localized_description_rows,
        "base_description_values": feature_summary.base_description_values,
        "base_description_words_total": feature_summary.base_description_words_total,
        "base_description_words_median": feature_summary.base_description_words_median,
        "localized_description_values": feature_summary.localized_description_values,
        "localized_description_words_total": feature_summary.localized_description_words_total,
        "localized_description_words_median": feature_summary.localized_description_words_median,
        "base_name_rows": feature_summary.base_name_rows,
        "localized_name_rows": feature_summary.localized_name_rows,
        "rejections": manifest_summary.rejections,
        "deduplicated_rows": manifest_summary.rejections.get("duplicate_osm_object", 0),
        "source_bytes_total": manifest_summary.source_bytes_total,
        "output_bytes_total": manifest_summary.output_bytes_total,
        "area_m2_count": feature_summary.rows,
        "area_m2_min_m2": feature_summary.area_min_m2,
        "area_m2_p25_m2": feature_summary.area_p25_m2,
        "area_m2_median_m2": feature_summary.area_median_m2,
        "area_m2_p75_m2": feature_summary.area_p75_m2,
        "area_m2_max_m2": feature_summary.area_max_m2,
        "data_min_timestamp_utc": feature_summary.data_min_timestamp_utc,
        "data_max_timestamp_utc": feature_summary.data_max_timestamp_utc,
        "files": manifest_summary.files,
    }


def collect_stats(
    data_root: Path,
    *,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Aggregate factual statistics from validated artifacts and matching manifests."""
    artifacts = _find_validated_artifacts(data_root)

    connection = _new_connection(data_root)
    try:
        _create_feature_table(connection)
        _ingest_features(connection, artifacts)
        feature_summary = _collect_feature_summary(connection)
    finally:
        connection.close()

    return _build_stats_payload(feature_summary, _collect_manifest_summary(artifacts))


__all__ = [
    "STATS_SCHEMA_VERSION",
    "ReportingError",
    "collect_stats",
    "utc_now_iso",
]
