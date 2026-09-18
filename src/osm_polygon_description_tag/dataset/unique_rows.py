"""Shared deterministic views of globally unique OSM identities."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.canonical_rows import (
    CANONICAL_FINGERPRINT_COLUMNS,
    CANONICAL_RANK_COLUMNS,
    canonical_geometry_wkb_sql,
    canonical_rows_sql,
)

_BATCH_SIZE = 4096
_REQUIRED_COLUMNS = frozenset(("source_pbf", "osm_type", "osm_id", "geometry"))
_OPTIONAL_RANK_COLUMN_TYPES = {
    "version": "INTEGER",
    "timestamp": "TIMESTAMP",
    "description": "VARCHAR",
}


class UniqueRowsError(RuntimeError):
    """Raised when a unique-row view cannot be read safely."""


def _require_batch_size(batch_size: int) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")


def _validate_input(data_root: Path, validate: bool) -> None:
    if not validate:
        return
    from osm_polygon_description_tag.dataset.storage import validate_finalized_artifacts

    validate_finalized_artifacts(data_root)


def _parquet_paths(data_root: Path) -> tuple[Path, ...]:
    return tuple(sorted((data_root / "data").glob("*.parquet"), key=lambda path: path.name))


def unique_rows_sql(
    relation: str,
    columns: Sequence[str],
    *,
    key_value_columns_are_maps: bool = False,
    require_successful_text: bool = False,
) -> str:
    """Return the canonical one-row-per-OSM-identity query for a relation."""
    options: dict[str, bool] = {
        "key_value_columns_are_maps": key_value_columns_are_maps,
    }
    if require_successful_text:
        options["require_successful_text"] = True
    return canonical_rows_sql(relation, columns, **options)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _missing_parquet_column_expression(
    column: str,
    *,
    path: Path,
    required_columns: frozenset[str],
) -> str:
    if column in {"localized_names", "localized_descriptions", "tags"}:
        if column in required_columns:
            raise UniqueRowsError(f"missing unique-row column {column!r} in {path}")
        return f"CAST([] AS STRUCT(key VARCHAR, value VARCHAR)[]) AS {column}"
    sql_type = _OPTIONAL_RANK_COLUMN_TYPES.get(column)
    if sql_type is not None:
        return f"CAST(NULL AS {sql_type}) AS {column}"
    if column in required_columns:
        raise UniqueRowsError(f"missing unique-row column {column!r} in {path}")
    return f"NULL AS {column}"


def _parquet_column_expression(
    column: str,
    available: set[str],
    *,
    has_geo_metadata: bool,
    path: Path,
    required_columns: frozenset[str] = _REQUIRED_COLUMNS,
) -> str:
    if column not in available:
        return _missing_parquet_column_expression(
            column,
            path=path,
            required_columns=required_columns,
        )
    if column == "geometry":
        return (
            f"{canonical_geometry_wkb_sql('geometry', input_is_geometry=has_geo_metadata)} "
            "AS geometry"
        )
    return column


def _parquet_select(
    path: Path,
    input_columns: Sequence[str],
    *,
    required_columns: frozenset[str] = _REQUIRED_COLUMNS,
) -> str:
    schema = pq.ParquetFile(path).schema_arrow
    available = set(schema.names)
    has_geo_metadata = bool(schema.metadata and b"geo" in schema.metadata)
    expressions = [
        _parquet_column_expression(
            column,
            available,
            has_geo_metadata=has_geo_metadata,
            path=path,
            required_columns=required_columns,
        )
        for column in input_columns
    ]
    parquet_literal = _sql_literal(str(path))
    return (
        f"SELECT {', '.join(expressions)} "  # noqa: S608 - internal SQL fragments
        f"FROM read_parquet({parquet_literal})"
    )


def _parquet_relation(paths: Sequence[Path], columns: Sequence[str]) -> str:
    input_columns = tuple(
        dict.fromkeys((*CANONICAL_RANK_COLUMNS, *CANONICAL_FINGERPRINT_COLUMNS, *columns))
    )
    required_columns = frozenset((*_REQUIRED_COLUMNS, *columns))
    return " UNION ALL ".join(
        _parquet_select(path, input_columns, required_columns=required_columns) for path in paths
    )


def _unique_rows_query(
    paths: Sequence[Path],
    columns: Sequence[str],
    *,
    require_successful_text: bool = False,
) -> str:
    options: dict[str, bool] = {}
    if require_successful_text:
        options["require_successful_text"] = True
    return unique_rows_sql(f"({_parquet_relation(paths, columns)})", columns, **options)


def _open_unique_rows_connection(data_root: Path):
    work_root = data_root / ".work" / "duckdb"
    work_root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    connection.execute("SET temp_directory = ?", [str(work_root)])
    return connection


def _read_unique_rows(
    connection: Any,
    query: str,
    batch_size: int,
    data_root: Path,
) -> Iterator[pa.RecordBatch]:
    try:
        reader = connection.execute(query).to_arrow_reader(batch_size)
        yield from reader
    except duckdb.Error as error:
        raise UniqueRowsError(
            f"cannot read unique Parquet rows under {data_root}: {error}"
        ) from error


def iter_unique_parquet_batches(
    data_root: Path,
    *,
    columns: Sequence[str],
    batch_size: int = _BATCH_SIZE,
    validate: bool = False,
    require_successful_text: bool = False,
) -> Iterator[pa.RecordBatch]:
    """Yield deterministic unique rows from finalized Parquet files.

    The ranking matches the repository's global deduplication policy. Only the
    requested columns plus the identity/ranking columns are read, and DuckDB's
    temp directory keeps the view disk-backed for large datasets.

    ``require_successful_text`` filters invalid, blank, and untrimmed text
    rows before canonical ranking so a rejected duplicate cannot hide a valid
    text row for the same identity.
    """
    _require_batch_size(batch_size)
    _validate_input(data_root, validate)
    paths = _parquet_paths(data_root)
    if not paths:
        return
    query_options: dict[str, bool] = {}
    if require_successful_text:
        query_options["require_successful_text"] = True
    query = _unique_rows_query(paths, columns, **query_options)
    connection = _open_unique_rows_connection(data_root)
    try:
        yield from _read_unique_rows(connection, query, batch_size, data_root)
    finally:
        connection.close()


__all__ = ["UniqueRowsError", "iter_unique_parquet_batches", "unique_rows_sql"]
