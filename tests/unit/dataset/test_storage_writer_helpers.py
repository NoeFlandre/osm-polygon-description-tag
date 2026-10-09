"""Behavioral coverage for the bounded GeoParquet validation helpers."""

import json
import os
from pathlib import Path
from unittest.mock import Mock, call, patch

import pyarrow as pa
import pytest

from osm_polygon_description_tag.dataset import (
    storage,
)
from osm_polygon_description_tag.dataset.storage import (
    _fsync_dir,
    _merge_bounds,
    _record_bounds,
    _RecordStreamSummary,
    _require_batch_size,
    _stream_records,
    _stream_rewrite_with_metadata,
    arrow_record,
    write_geoparquet,
)
from tests.helpers.messages import exactly


def test_arrow_record_preserves_scalars_and_normalizes_key_value_columns() -> None:
    record = {
        "source_pbf": "region.osm.pbf",
        "localized_descriptions": {"fr": "Bonjour"},
        "tags": {"description": "Hello"},
    }

    assert arrow_record(record) == {
        "source_pbf": "region.osm.pbf",
        "localized_descriptions": [{"key": "fr", "value": "Bonjour"}],
        "localized_names": [],
        "tags": [{"key": "description", "value": "Hello"}],
    }


def test_record_bounds_reads_all_four_coordinates_as_floats() -> None:
    record = {
        "bbox_min_x": "-1.5",
        "bbox_min_y": 2,
        "bbox_max_x": 3.25,
        "bbox_max_y": "4.75",
    }

    assert _record_bounds(record) == (-1.5, 2.0, 3.25, 4.75)


def test_merge_bounds_handles_first_update_and_each_extent_direction() -> None:
    first = (10.0, 20.0, 30.0, 40.0)
    second = (15.0, 5.0, 35.0, 45.0)

    assert _merge_bounds(None, first) == first
    assert _merge_bounds(first, second) == (10.0, 5.0, 35.0, 45.0)


class _BatchRecorder:
    def __init__(self) -> None:
        self.batches: list[pa.RecordBatch] = []

    def write_batch(self, batch: pa.RecordBatch) -> None:
        self.batches.append(batch)


def test_stream_records_reports_summary_and_writes_exact_batch_sizes(
    valid_records: list[dict[str, object]],
) -> None:
    writer = _BatchRecorder()

    summary = _stream_records(iter(valid_records), writer, batch_size=1)

    assert summary.row_count == len(valid_records)
    assert summary.geometry_types == frozenset({"Polygon", "MultiPolygon"})
    assert summary.bbox == (0.0, 0.0, 21.0, 21.0)
    assert [batch.num_rows for batch in writer.batches] == [1, 1]


def test_stream_records_writes_an_empty_schema_batch_for_empty_input() -> None:
    writer = _BatchRecorder()

    summary = _stream_records(iter(()), writer, batch_size=1)

    assert summary.row_count == 0
    assert summary.geometry_types == frozenset()
    assert summary.bbox is None
    assert [batch.num_rows for batch in writer.batches] == [0]


@pytest.mark.parametrize("batch_size", [0, -1])
def test_require_batch_size_rejects_non_positive_values(batch_size: int) -> None:
    with pytest.raises(ValueError, match=exactly("batch_size must be positive")):
        _require_batch_size(batch_size)


def test_require_batch_size_uses_the_exact_error_message() -> None:
    with pytest.raises(ValueError) as error:
        _require_batch_size(0)

    assert str(error.value) == "batch_size must be positive"


def test_require_batch_size_accepts_one() -> None:
    _require_batch_size(1)


def test_fsync_dir_uses_the_owned_directory_and_closes_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[object, object]] = []
    synced: list[int] = []
    closed: list[int] = []

    def open_directory(path: str, flags: int, **_kwargs: object) -> int:
        opened.append((path, flags))
        return 17

    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))

    _fsync_dir(tmp_path)

    assert opened == [(str(tmp_path), os.O_RDONLY)]
    assert synced == [17]
    assert closed == [17]


def test_write_geoparquet_uses_contract_writer_options_and_default_batch_size(
    tmp_path: Path,
) -> None:
    target = tmp_path / "region.parquet"
    temp_data = tmp_path / ".region.data.tmp"
    temp_final = tmp_path / ".region.final.tmp"
    records = object()
    summary = _RecordStreamSummary(0, frozenset(), None)
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=None)
    validator = Mock(return_value=7)

    with (
        patch.object(storage, "_owned_temp", side_effect=[temp_data, temp_final]),
        patch.object(storage.pq, "ParquetWriter", return_value=writer) as writer_factory,
        patch.object(storage, "_stream_records", return_value=summary) as stream,
        patch.object(storage, "_stream_rewrite_with_metadata") as rewrite,
        patch.object(storage, "fsync_file") as fsync_file,
        patch.object(storage, "_fsync_dir") as fsync_dir,
        patch.object(os, "replace") as replace,
    ):
        ordered = Mock()
        ordered.attach_mock(fsync_file, "fsync_file")
        ordered.attach_mock(replace, "replace")
        ordered.attach_mock(fsync_dir, "fsync_dir")
        assert write_geoparquet(records, target, validator=validator) == 7

    writer_factory.assert_called_once_with(
        temp_data,
        storage.SCHEMA,
        compression="zstd",
        use_dictionary=storage.DICTIONARY_COLUMNS,
    )
    stream.assert_called_once_with(records, writer, 1024)
    rewrite.assert_called_once_with(
        temp_data,
        temp_final,
        geometry_types=[],
        bbox=[],
    )
    validator.assert_called_once_with(temp_final)
    fsync_file.assert_called_once_with(temp_final)
    fsync_dir.assert_called_once_with(tmp_path)
    replace.assert_called_once_with(temp_final, target)
    # The directory entry only becomes durable after the rename, so the
    # directory fsync must come last.
    assert ordered.mock_calls == [
        call.fsync_file(temp_final),
        call.replace(temp_final, target),
        call.fsync_dir(tmp_path),
    ]


def test_stream_rewrite_passes_exact_metadata_writer_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.parquet"
    target = tmp_path / "target.parquet"
    batch = object()
    reader = Mock()
    reader.iter_batches.return_value = [batch]
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=None)
    writer_factory = Mock(return_value=writer)
    reader_factory = Mock(return_value=reader)
    monkeypatch.setattr(storage.pq, "ParquetFile", reader_factory)
    monkeypatch.setattr(storage.pq, "ParquetWriter", writer_factory)

    _stream_rewrite_with_metadata(
        source,
        target,
        geometry_types=["Polygon"],
        bbox=[0.0, 1.0, 2.0, 3.0],
    )

    reader_factory.assert_called_once_with(source)
    writer_factory.assert_called_once()
    writer_args, writer_kwargs = writer_factory.call_args
    assert writer_args[0] == target
    assert json.loads(writer_args[1].metadata[b"geo"]) == {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Polygon"],
                "bbox": [0.0, 1.0, 2.0, 3.0],
                "covering": {
                    "bbox": {
                        "xmin": ["bbox_min_x"],
                        "ymin": ["bbox_min_y"],
                        "xmax": ["bbox_max_x"],
                        "ymax": ["bbox_max_y"],
                    }
                },
            }
        },
    }
    assert writer_kwargs == {"compression": "zstd", "use_dictionary": storage.DICTIONARY_COLUMNS}
    reader.iter_batches.assert_called_once_with(batch_size=4096)
    writer.write_batch.assert_called_once_with(batch)
