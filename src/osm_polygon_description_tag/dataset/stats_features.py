"""DuckDB-backed feature aggregates for dataset statistics.

Every validated Parquet batch is streamed into one in-memory table. The unique
feature view then yields the row counts, tag and text coverage, area quantiles,
word statistics, and timestamp bounds that stats.json reports.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.canonical_rows import canonical_geometry_wkb_sql
from osm_polygon_description_tag.dataset.schema import SCHEMA
from osm_polygon_description_tag.dataset.stats_manifest import ReportingError, ValidatedArtifact
from osm_polygon_description_tag.dataset.text import successful_description_text_sql
from osm_polygon_description_tag.dataset.unique_rows import unique_rows_sql

_QUANTILE_PROBABILITIES = [0.25, 0.5, 0.75]

_FEATURE_COLUMNS = list(SCHEMA.names)


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
class FeatureSummary:
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
    area_total_m2: float = 0.0
    area_mean_m2: float | None = None
    dataset_bbox: tuple[float, float, float, float] | None = None
    geometry_vertices_total: int = 0
    geometry_rings_total: int = 0
    geometry_holes_total: int = 0
    multipolygon_components_total: int = 0
    raw_rows: int | None = None
    raw_successful_text_rows: int | None = None
    all_unique_osm_objects: int | None = None


def create_feature_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE all_features (
            source_pbf VARCHAR NOT NULL,
            osm_type VARCHAR NOT NULL,
            osm_id BIGINT NOT NULL,
            osm_url VARCHAR NOT NULL,
            version INTEGER,
            changeset BIGINT,
            timestamp TIMESTAMP,
            name VARCHAR,
            localized_names MAP(VARCHAR, VARCHAR) NOT NULL,
            description VARCHAR,
            localized_descriptions MAP(VARCHAR, VARCHAR) NOT NULL,
            tags MAP(VARCHAR, VARCHAR) NOT NULL,
            geometry_type VARCHAR NOT NULL,
            area_m2 DOUBLE NOT NULL,
            bbox_min_x DOUBLE NOT NULL,
            bbox_min_y DOUBLE NOT NULL,
            bbox_max_x DOUBLE NOT NULL,
            bbox_max_y DOUBLE NOT NULL,
            geometry BLOB NOT NULL
        )
        """
    )


def create_unique_feature_view(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        "CREATE TEMP VIEW features AS "
        + unique_rows_sql(
            "all_features",
            _FEATURE_COLUMNS,
            key_value_columns_are_maps=True,
            require_successful_text=True,
        )
    )


def _insert_batch(
    connection: duckdb.DuckDBPyConnection,
    batch: pa.RecordBatch,
    _source_name: str,
) -> None:
    localized_names_sql = _map_sql_expression(batch, "localized_names")
    localized_descriptions_sql = _map_sql_expression(batch, "localized_descriptions")
    tags_sql = _map_sql_expression(batch, "tags")
    # DuckDB folds identifier case, including inside quotes, so re-casing this
    # column name produces the same query.
    geometry_sql = canonical_geometry_wkb_sql("geometry")  # pragma: no mutate
    connection.register("batch", batch)
    try:
        connection.execute(
            f"""
            INSERT INTO all_features
            SELECT
                source_pbf,
                osm_type,
                osm_id,
                osm_url,
                version,
                changeset,
                timestamp,
                name,
                CASE WHEN {localized_names_sql} IS NULL
                     THEN MAP() ELSE {localized_names_sql} END,
                description,
                CASE WHEN {localized_descriptions_sql} IS NULL
                     THEN MAP() ELSE {localized_descriptions_sql} END,
                CASE WHEN {tags_sql} IS NULL
                     THEN MAP() ELSE {tags_sql} END,
                geometry_type,
                area_m2,
                bbox_min_x,
                bbox_min_y,
                bbox_max_x,
                bbox_max_y,
                {geometry_sql}
            FROM batch
            """,  # noqa: S608 - expressions are internal fixed column names
        )
    finally:
        connection.unregister("batch")


def ingest_features(
    connection: duckdb.DuckDBPyConnection,
    artifacts: tuple[ValidatedArtifact, ...],
) -> None:
    """Stream every finalized Parquet batch into the feature table."""
    for artifact in artifacts:
        file_reader = pq.ParquetFile(artifact.parquet)
        for batch in file_reader.iter_batches(
            columns=_FEATURE_COLUMNS,
            batch_size=4096,
        ):
            _insert_batch(connection, batch, artifact.parquet.name)


def query_int(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    result = connection.execute(query).fetchone()
    return int(result[0] if result else 0)


def _ordered_counts(connection: duckdb.DuckDBPyConnection, query: str) -> dict[str, int]:
    return {str(key): int(value) for key, value in connection.execute(query).fetchall()}


def collect_feature_summary(connection: duckdb.DuckDBPyConnection) -> FeatureSummary:
    all_unique_osm_objects = query_int(
        connection,
        "SELECT COUNT(*) FROM (SELECT DISTINCT osm_type, osm_id FROM all_features)",
    )
    rows = query_int(connection, "SELECT COUNT(*) FROM features")
    raw_successful_text_rows = query_int(
        connection,
        "SELECT COUNT(*) FROM all_features WHERE "  # noqa: S608 - internal fixed columns
        + successful_description_text_sql(localized_is_map=True),
    )
    unique_osm_objects = query_int(
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
    base_description_rows = query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE description IS NOT NULL",
    )
    localized_description_rows = query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE cardinality(localized_descriptions) > 0",
    )
    base_name_rows = query_int(
        connection,
        "SELECT COUNT(*) FROM features WHERE name IS NOT NULL",
    )
    localized_name_rows = query_int(
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
    return FeatureSummary(
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
        raw_successful_text_rows=raw_successful_text_rows,
        all_unique_osm_objects=all_unique_osm_objects,
    )
