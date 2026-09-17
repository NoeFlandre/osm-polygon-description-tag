"""Unit tests for deterministic Parquet relation construction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import osm_polygon_description_tag.dataset.unique_rows as unique_rows


def test_parquet_column_expression_normalizes_geoparquet_geometry() -> None:
    assert (
        unique_rows._parquet_column_expression(
            "geometry",
            {"geometry"},
            has_geo_metadata=True,
            path=Path("region.parquet"),
        )
        == "ST_AsWKB(geometry) AS geometry"
    )


@pytest.mark.parametrize(
    ("column", "available", "has_geo_metadata", "expected"),
    [
        ("geometry", {"geometry"}, False, "geometry"),
        ("version", set(), False, "CAST(NULL AS INTEGER) AS version"),
        ("timestamp", set(), False, "CAST(NULL AS TIMESTAMP) AS timestamp"),
        ("description", set(), False, "CAST(NULL AS VARCHAR) AS description"),
    ],
)
def test_parquet_column_expression_handles_non_geospatial_and_optional_columns(
    column: str,
    available: set[str],
    has_geo_metadata: bool,
    expected: str,
) -> None:
    assert (
        unique_rows._parquet_column_expression(
            column,
            available,
            has_geo_metadata=has_geo_metadata,
            path=Path("region.parquet"),
        )
        == expected
    )


def test_parquet_column_expression_rejects_missing_required_column() -> None:
    with pytest.raises(unique_rows.UniqueRowsError, match="missing unique-row column 'osm_id'"):
        unique_rows._parquet_column_expression(
            "osm_id",
            set(),
            has_geo_metadata=False,
            path=Path("region.parquet"),
        )


def test_parquet_select_reads_schema_once_and_builds_pruned_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = SimpleNamespace(
        names=["source_pbf", "osm_type", "osm_id", "geometry"],
        metadata={b"geo": b"{}"},
    )
    parquet_file = SimpleNamespace(schema_arrow=schema)
    monkeypatch.setattr(unique_rows.pq, "ParquetFile", lambda _path: parquet_file)

    sql = unique_rows._parquet_select(
        Path("region's.parquet"),
        ("source_pbf", "osm_type", "osm_id", "version", "geometry"),
    )

    assert "CAST(NULL AS INTEGER) AS version" in sql
    assert "ST_AsWKB(geometry) AS geometry" in sql
    assert "FROM read_parquet('region''s.parquet')" in sql
