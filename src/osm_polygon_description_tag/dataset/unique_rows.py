"""Shared deterministic views of globally unique OSM identities."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.schema import SCHEMA

_BATCH_SIZE = 4096
_RANK_COLUMNS = (
    "source_pbf",
    "osm_type",
    "osm_id",
    "version",
    "timestamp",
    "description",
    "geometry",
)
_OPTIONAL_RANK_COLUMN_TYPES = {
    "version": "INTEGER",
    "timestamp": "TIMESTAMP",
    "description": "VARCHAR",
}


class UniqueRowsError(RuntimeError):
    """Raised when a unique-row view cannot be read safely."""


def unique_rows_sql(relation: str, columns: Sequence[str]) -> str:
    """Return the canonical one-row-per-OSM-identity query for a relation."""
    selected = tuple(dict.fromkeys(columns))
    if not selected:
        raise ValueError("unique-row views require at least one selected column")
    unknown = set((*selected, *_RANK_COLUMNS)) - set(SCHEMA.names)
    if unknown:
        raise ValueError(f"unsupported unique-row columns: {sorted(unknown)}")
    selected_sql = ", ".join(selected)
    return f"""
        SELECT {selected_sql}
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY osm_type, osm_id
                ORDER BY version DESC NULLS LAST,
                         timestamp DESC NULLS LAST,
                         source_pbf ASC,
                         md5(concat_ws('|',
                             coalesce(cast(version AS VARCHAR), ''),
                             coalesce(cast(timestamp AS VARCHAR), ''),
                             source_pbf,
                             coalesce(description, ''),
                             hex(geometry)
                         )) ASC
            ) AS _unique_rank
            FROM {relation}
        ) ranked
        WHERE _unique_rank = 1
    """  # noqa: S608 - relation/columns are internal allowlisted SQL fragments


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parquet_relation(paths: Sequence[Path], columns: Sequence[str]) -> str:
    input_columns = tuple(dict.fromkeys((*_RANK_COLUMNS, *columns)))
    selects: list[str] = []
    for path in paths:
        schema = pq.ParquetFile(path).schema_arrow
        available = set(schema.names)
        has_geo_metadata = bool(schema.metadata and b"geo" in schema.metadata)
        expressions: list[str] = []
        for column in input_columns:
            if column in available:
                if column == "geometry" and has_geo_metadata:
                    expressions.append("ST_AsWKB(geometry) AS geometry")
                else:
                    expressions.append(column)
                continue
            sql_type = _OPTIONAL_RANK_COLUMN_TYPES.get(column)
            if sql_type is None:
                raise UniqueRowsError(f"missing unique-row column {column!r} in {path}")
            expressions.append(f"CAST(NULL AS {sql_type}) AS {column}")
        parquet_literal = _sql_literal(str(path))
        select_sql = (
            f"SELECT {', '.join(expressions)} "  # noqa: S608 - internal SQL fragments
            f"FROM read_parquet({parquet_literal})"
        )
        selects.append(select_sql)
    return " UNION ALL ".join(selects)


def iter_unique_parquet_batches(
    data_root: Path,
    *,
    columns: Sequence[str],
    batch_size: int = _BATCH_SIZE,
    validate: bool = False,
) -> Iterator[pa.RecordBatch]:
    """Yield deterministic unique rows from finalized Parquet files.

    The ranking matches the repository's global deduplication policy. Only the
    requested columns plus the identity/ranking columns are read, and DuckDB's
    temp directory keeps the view disk-backed for large datasets.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if validate:
        from osm_polygon_description_tag.dataset.storage import validate_finalized_artifacts

        validate_finalized_artifacts(data_root)
    paths = tuple(sorted((data_root / "data").glob("*.parquet"), key=lambda path: path.name))
    if not paths:
        return
    query = unique_rows_sql(f"({_parquet_relation(paths, columns)})", columns)
    work_root = data_root / ".work" / "duckdb"
    work_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    connection.execute("SET temp_directory = ?", [str(work_root)])
    try:
        reader = connection.execute(query).to_arrow_reader(batch_size)
        yield from reader
    except duckdb.Error as error:
        raise UniqueRowsError(
            f"cannot read unique Parquet rows under {data_root}: {error}"
        ) from error
    finally:
        connection.close()


__all__ = ["UniqueRowsError", "iter_unique_parquet_batches", "unique_rows_sql"]
