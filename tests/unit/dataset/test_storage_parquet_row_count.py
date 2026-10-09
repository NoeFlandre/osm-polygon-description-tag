"""Contracts for reading the footer row count of a finalized Parquet artifact."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.storage import StorageError, _read_parquet_row_count


@pytest.mark.parametrize("rows", [0, 1, 3], ids=["empty", "one-row", "three-rows"])
def test_read_parquet_row_count_returns_the_footer_row_count_of_a_real_file(
    tmp_path: Path, rows: int
) -> None:
    parquet = tmp_path / "region.parquet"
    pq.write_table(pa.table({"description": pa.array(["text"] * rows, pa.string())}), parquet)

    assert _read_parquet_row_count(parquet) == rows


def test_read_parquet_row_count_refuses_a_corrupt_file_with_its_path_and_cause(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "region.parquet"
    parquet.write_bytes(b"tiny parquet fixture")

    with pytest.raises(StorageError) as error:
        _read_parquet_row_count(parquet)

    assert isinstance(error.value.__cause__, pa.ArrowException)
    assert str(error.value) == f"cannot read finalized artifact {parquet}: {error.value.__cause__}"


def test_read_parquet_row_count_refuses_a_missing_file_with_its_path_and_cause(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "missing.parquet"

    with pytest.raises(StorageError) as error:
        _read_parquet_row_count(parquet)

    assert isinstance(error.value.__cause__, OSError)
    assert str(error.value) == f"cannot read finalized artifact {parquet}: {error.value.__cause__}"
