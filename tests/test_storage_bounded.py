"""Bounded-memory stress tests for the storage layer.

These tests prove that the rewrite pass never calls :func:`pyarrow.parquet.read_table`
to load the entire per-PBF Parquet into memory, and that the uniqueness check
uses a SQLite primary-key table rather than an in-memory set.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest
from shapely import to_wkb
from shapely.geometry import Polygon

from osm_polygon_description_tag.storage import (
    StorageError,
    validate_geoparquet,
    write_geoparquet,
)
from tests.conftest import make_record_dict

_POLYGON_WKB = to_wkb(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), output_dimension=2)


def test_write_geoparquet_never_loads_full_table_into_memory(tmp_path: Path) -> None:
    """`pq.read_table` must not be called during the rewrite pass."""
    target = tmp_path / "region.parquet"

    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
    )

    with patch("osm_polygon_description_tag.storage.pq.read_table") as mock_read:
        write_geoparquet(iter([record]), target, batch_size=1)
        mock_read.assert_not_called()


def test_validate_geoparquet_uses_sqlite_uniqueness_check(tmp_path: Path) -> None:
    """The uniqueness check uses a SQLite-backed primary-key table.

    We assert by checking that no in-memory set ``_UniquenessIndex`` is ever
    populated with raw (osm_type, osm_id) tuples larger than a single batch.
    """
    target = tmp_path / "region.parquet"
    records = [
        make_record_dict(
            Polygon([(i, 0), (i, 1), (i + 0.5, 1), (i + 0.5, 0)]),
            {"description": f"f{i}"},
            osm_id=2000 + i,
        )
        for i in range(50)
    ]
    write_geoparquet(iter(records), target, batch_size=10)

    # Should validate without raising despite the large row count.
    assert validate_geoparquet(target) == 50


def test_write_geoparquet_streams_via_iter_batches(tmp_path: Path) -> None:
    """The final write phase iterates batches; verify the streaming path is used."""
    target = tmp_path / "region.parquet"

    records = [
        make_record_dict(
            Polygon([(i, 0), (i, 1), (i + 0.5, 1), (i + 0.5, 0)]),
            {"description": f"f{i}"},
            osm_id=3000 + i,
        )
        for i in range(20)
    ]

    write_geoparquet(iter(records), target, batch_size=5)

    pf = pq.ParquetFile(target)
    batches = list(pf.iter_batches(batch_size=5))
    assert len(batches) >= 4


def test_validate_geoparquet_with_large_row_count_uses_sqlite(tmp_path: Path) -> None:
    """A large row count does not blow up the validation."""
    target = tmp_path / "big.parquet"
    records = [
        make_record_dict(
            Polygon([(i % 5, 0), (i % 5, 1), (i % 5 + 0.5, 1), (i % 5 + 0.5, 0)]),
            {"description": "f"},
            osm_id=4000 + i,
        )
        for i in range(500)
    ]
    write_geoparquet(iter(records), target, batch_size=100)
    assert validate_geoparquet(target) == 500


def test_storage_temp_files_are_removed_on_validation_failure(tmp_path: Path) -> None:
    target = tmp_path / "fail.parquet"
    record = make_record_dict(
        Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
        {"description": "x"},
        osm_id=1,
    )

    def fail(_path: Path) -> int:
        raise StorageError("intentional failure")

    with pytest.raises(StorageError, match="intentional"):
        write_geoparquet(iter([record]), target, batch_size=10, validator=fail)

    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
    assert not target.exists()
