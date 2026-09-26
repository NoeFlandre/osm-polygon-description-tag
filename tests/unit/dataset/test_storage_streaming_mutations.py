"""Focused streaming contracts for the Arrow GeoParquet writer fast paths."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pytest
from shapely.geometry import MultiPolygon, Polygon

import osm_polygon_description_tag.dataset.storage as storage
from osm_polygon_description_tag.dataset.constants import DEFAULT_WRITE_BATCH_SIZE
from osm_polygon_description_tag.dataset.schema import SCHEMA
from tests.conftest import make_record_dict

_PAIR_LIST = pa.list_(pa.struct([("key", pa.string()), ("value", pa.string())]))


def _key_list(items: list[list[tuple[str, str]]]) -> pa.ListArray:
    return pa.array(
        [[{"key": key, "value": value} for key, value in group] for group in items],
        type=_PAIR_LIST,
    )


def _record(osm_id: int, geometry: Polygon | MultiPolygon) -> dict[str, object]:
    return make_record_dict(
        geometry,
        {"description": f"feature {osm_id}"},
        osm_id=osm_id,
        source_pbf="storage-test.osm.pbf",
    )


def _batch(records: list[dict[str, object]]) -> pa.RecordBatch:
    return pa.RecordBatch.from_pylist(
        [storage._arrow_record(record) for record in records], schema=SCHEMA
    )


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[int]]] = []

    def write_batch(self, batch: pa.RecordBatch) -> None:
        self.calls.append(("batch", batch.num_rows, batch.column("osm_id").to_pylist()))

    def write_table(self, table: pa.Table) -> None:
        self.calls.append(("table", table.num_rows, table.column("osm_id").to_pylist()))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_key_order_check_resets_boundaries_for_a_sliced_list_array() -> None:
    # The first retained list begins after a nonzero offset. Its singleton key
    # must not be compared with the first key in the next list.
    sliced = _key_list([[("skip", "0")], [("m", "1")], [("a", "2"), ("b", "3")]]).slice(1)

    assert storage._pairs_are_canonical(sliced)


def test_key_order_check_accepts_singleton_lists_through_the_final_offset() -> None:
    # Both lists are individually ordered, even though the flattened keys fall
    # from z to a at their shared boundary and the last offset equals len(keys).
    singleton_lists = _key_list([[("z", "1")], [("a", "2")]])

    assert storage._pairs_are_canonical(singleton_lists)


def test_key_order_check_does_not_skip_a_bad_pair_after_an_empty_first_list() -> None:
    values = _key_list([[], [("z", "1"), ("a", "2")]])

    assert not storage._pairs_are_canonical(values)


def test_stream_batches_writes_one_empty_batch_and_returns_empty_summary() -> None:
    writer = _RecordingWriter()

    summary = storage._stream_batches([], writer, batch_size=2)  # type: ignore[arg-type]

    assert writer.calls == [("batch", 0, [])]
    assert summary.row_count == 0
    assert summary.geometry_types == frozenset()
    assert summary.bbox is None


def test_stream_batches_rechunks_nonempty_batches_and_summarizes_rows() -> None:
    first = _record(10, Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]))
    second = _record(
        11,
        MultiPolygon(
            [
                Polygon([(10, 0), (10, 2), (12, 2), (12, 0)]),
                Polygon([(14, 0), (14, 1), (15, 1), (15, 0)]),
            ]
        ),
    )
    third = _record(12, Polygon([(20, 0), (20, 1), (21, 1), (21, 0)]))
    writer = _RecordingWriter()

    summary = storage._stream_batches(
        [_batch([first]), _batch([]), _batch([second, third])],
        writer,  # type: ignore[arg-type]
        batch_size=2,
    )

    assert writer.calls == [("batch", 2, [10, 11]), ("table", 1, [12])]
    assert summary.row_count == 3
    assert summary.geometry_types == frozenset({"Polygon", "MultiPolygon"})
    assert summary.bbox == (0.0, 0.0, 21.0, 2.0)


def test_batches_default_matches_row_writer_batch_size_and_bytes(tmp_path: Path) -> None:
    records = [
        _record(index + 1, Polygon([(index, 0), (index, 1), (index + 1, 1), (index + 1, 0)]))
        for index in range(DEFAULT_WRITE_BATCH_SIZE + 1)
    ]
    row_path = tmp_path / "rows.parquet"
    batch_path = tmp_path / "batches.parquet"

    row_count = storage.write_geoparquet(records, row_path)
    batch_count = storage.write_geoparquet_batches([_batch(records)], batch_path)

    assert row_count == batch_count == DEFAULT_WRITE_BATCH_SIZE + 1
    assert _sha256(row_path) == _sha256(batch_path)


def test_batches_passes_the_row_writer_default_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[int] = []

    def capture_batch_size(_stream, _target, *, batch_size, validator):  # type: ignore[no-untyped-def]
        observed.append(batch_size)
        return 0

    monkeypatch.setattr(storage, "_write_geoparquet_with", capture_batch_size)

    storage.write_geoparquet_batches([], tmp_path / "unused.parquet")

    assert observed == [DEFAULT_WRITE_BATCH_SIZE]


def test_batches_default_validator_rejects_duplicate_identities(tmp_path: Path) -> None:
    duplicate = _record(42, Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]))
    target = tmp_path / "duplicates.parquet"

    with pytest.raises(storage.StorageError, match="duplicate"):
        storage.write_geoparquet_batches([_batch([duplicate, duplicate])], target)

    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_batches_passes_the_custom_validator_and_returns_its_row_summary(
    tmp_path: Path,
) -> None:
    target = tmp_path / "custom-validator.parquet"
    calls: list[Path] = []

    def summarize(path: Path) -> int:
        assert path.is_file()
        calls.append(path)
        return 71

    result = storage.write_geoparquet_batches(
        [_batch([_record(7, Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]))])],
        target,
        validator=summarize,
    )

    assert result == 71
    assert len(calls) == 1
    assert calls[0] != target
    assert target.is_file()
