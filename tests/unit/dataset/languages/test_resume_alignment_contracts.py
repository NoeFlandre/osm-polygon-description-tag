"""Exact row-group selection contracts behind a resumed shard.

`ParquetFile.iter_batches` restarts its batch grid at the first selected row
group, so resuming from the wrong group silently shifts every batch boundary.
Uninterrupted and resumed output have to be byte-identical, which makes this
arithmetic part of the published contract rather than an implementation detail.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_description_tag.dataset.languages.worker import _starting_row_groups


def _metadata(tmp_path: Path, row_group_sizes: list[int]) -> pq.FileMetaData:
    path = tmp_path / "shard.parquet"
    schema = pa.schema([pa.field("value", pa.int64())])
    with pq.ParquetWriter(path, schema) as writer:
        for size in row_group_sizes:
            writer.write_table(pa.table({"value": list(range(size))}, schema=schema))
    return pq.ParquetFile(path).metadata


def test_resuming_at_row_zero_selects_every_row_group(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, [4, 4, 4])

    assert _starting_row_groups(metadata, 0, 4) == ([0, 1, 2], 0)


def test_the_selection_starts_at_the_latest_batch_aligned_boundary(tmp_path: Path) -> None:
    """Row group 2 starts at row 8, which is on the grid for batch size 4."""
    metadata = _metadata(tmp_path, [4, 4, 4])

    assert _starting_row_groups(metadata, 8, 4) == ([2], 8)


def test_a_row_group_boundary_off_the_grid_falls_back_to_the_last_aligned_one(
    tmp_path: Path,
) -> None:
    """Groups start at rows 0, 5 and 10; with batch size 4 only row 0 is aligned.

    Resuming must therefore rewind to group 0 and replay from row 0 rather than
    start a fresh batch grid at row 5.
    """
    metadata = _metadata(tmp_path, [5, 5, 5])

    assert _starting_row_groups(metadata, 7, 4) == ([0, 1, 2], 0)


def test_a_start_row_inside_the_first_group_selects_the_whole_file(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, [4, 4])

    assert _starting_row_groups(metadata, 3, 4) == ([0, 1], 0)


def test_a_start_row_exactly_on_a_group_boundary_skips_the_earlier_groups(
    tmp_path: Path,
) -> None:
    """The comparison is strict: a group ending exactly at the cursor is done."""
    metadata = _metadata(tmp_path, [4, 4, 4])

    assert _starting_row_groups(metadata, 4, 4) == ([1, 2], 4)


def test_a_start_row_at_or_past_the_end_selects_nothing_and_reports_the_total(
    tmp_path: Path,
) -> None:
    metadata = _metadata(tmp_path, [4, 4])

    assert _starting_row_groups(metadata, 8, 4) == ([], 8)
    assert _starting_row_groups(metadata, 99, 4) == ([], 8)


@pytest.mark.parametrize("batch_size", [1, 2, 4])
def test_the_reported_offset_is_always_on_the_batch_grid(tmp_path: Path, batch_size: int) -> None:
    metadata = _metadata(tmp_path, [4, 4, 4])

    for start_row in range(13):
        _, aligned_offset = _starting_row_groups(metadata, start_row, batch_size)
        assert aligned_offset % batch_size == 0
        assert aligned_offset <= start_row or aligned_offset == 12
