"""Vectorized fast paths must agree exactly with the per-row code they shortcut.

Every fast path either returns what the per-row pass would have returned or
declines (returns ``None``/falls back) so that pass alone produces errors.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pyarrow as pa
import pytest
from shapely.geometry import LineString, MultiPolygon, Polygon

import osm_polygon_description_tag.dataset.geography.parquet_inputs as parquet_inputs
import osm_polygon_description_tag.dataset.stats as stats_module
import osm_polygon_description_tag.dataset.storage as storage
from osm_polygon_description_tag.dataset.geography import area_histogram
from osm_polygon_description_tag.dataset.schema import SCHEMA
from tests.conftest import make_record_dict

_SQUARE = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
_HOLED = Polygon(
    [(10, 10), (10, 20), (20, 20), (20, 10)],
    [[(12, 12), (12, 14), (14, 14), (14, 12)]],
)
_MULTI = MultiPolygon([_SQUARE, Polygon([(5, 5), (5, 6), (6, 6), (6, 5)])])


def _records(count: int, *, zero_x: bool = False) -> list[dict[str, object]]:
    records = []
    for index in range(count):
        offset = float(index % 7)
        shape = Polygon([(offset, 0), (offset, 1), (offset + 1, 1), (offset + 1, 0)])
        if zero_x and index == 1:
            shape = Polygon([(-0.0, 0), (-0.0, 1), (1, 1), (1, 0)])
        tags = {"description": f"text {index}", "name": f"n{index}", "building": "yes"}
        records.append(make_record_dict(shape, tags, osm_id=index + 1, source_pbf="a.osm.pbf"))
    return records


def _arrow_batch(records: list[dict[str, object]]) -> pa.RecordBatch:
    return pa.RecordBatch.from_pylist(
        [storage._arrow_record(record) for record in records], schema=SCHEMA
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- storage: write_geoparquet_batches -------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 3, 5, 64])
def test_write_geoparquet_batches_is_byte_identical_to_row_writing(
    tmp_path: Path, chunk: int
) -> None:
    records = _records(11, zero_x=True)
    batch = _arrow_batch(records)
    slices = [batch.slice(start, chunk) for start in range(0, batch.num_rows, chunk)]
    rows_target = tmp_path / "rows.parquet"
    batch_target = tmp_path / "batches.parquet"

    assert storage.write_geoparquet(batch.to_pylist(), rows_target, batch_size=4) == 11
    assert (
        storage.write_geoparquet_batches(
            [pa.RecordBatch.from_pylist([], schema=SCHEMA), *slices], batch_target, batch_size=4
        )
        == 11
    )

    assert _sha(rows_target) == _sha(batch_target)


def test_write_geoparquet_batches_matches_rows_for_non_canonical_pairs(tmp_path: Path) -> None:
    record = storage._arrow_record(_records(1)[0])
    record["tags"] = [{"key": "z", "value": "1"}, {"key": "a", "value": None}]
    batch = pa.RecordBatch.from_pylist([record], schema=SCHEMA)
    rows_target = tmp_path / "rows.parquet"
    batch_target = tmp_path / "batches.parquet"
    no_validation = {"validator": lambda _path: 0}

    storage.write_geoparquet(batch.to_pylist(), rows_target, **no_validation)
    storage.write_geoparquet_batches([batch], batch_target, **no_validation)

    assert _sha(rows_target) == _sha(batch_target)


def test_write_geoparquet_batches_writes_an_empty_file_like_rows(tmp_path: Path) -> None:
    rows_target = tmp_path / "rows.parquet"
    batch_target = tmp_path / "batches.parquet"
    no_validation = {"validator": lambda _path: 0}

    storage.write_geoparquet([], rows_target, **no_validation)
    storage.write_geoparquet_batches([], batch_target, **no_validation)

    assert _sha(rows_target) == _sha(batch_target)


def test_write_geoparquet_batches_rejects_a_non_positive_batch_size(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be positive"):
        storage.write_geoparquet_batches([], tmp_path / "x.parquet", batch_size=0)
    assert not (tmp_path / "x.parquet").exists()


def test_key_value_list_type_check() -> None:
    loose = pa.list_(pa.struct([("key", pa.string()), ("value", pa.string())]))
    assert storage._is_key_value_list(loose)
    assert storage._is_key_value_list(SCHEMA.field("tags").type)
    assert not storage._is_key_value_list(pa.string())
    assert not storage._is_key_value_list(pa.list_(pa.string()))
    assert not storage._is_key_value_list(
        pa.list_(pa.struct([("value", pa.string()), ("key", pa.string())]))
    )
    assert not storage._is_key_value_list(
        pa.list_(pa.struct([("key", pa.string()), ("value", pa.int64())]))
    )


def _pairs(values: list[object]) -> pa.Array:
    entry = pa.struct([("key", pa.string()), ("value", pa.string())])
    return pa.array(values, type=pa.list_(entry))


def _kv(*pairs: tuple[str | None, str | None]) -> list[dict[str, str | None]]:
    return [{"key": key, "value": value} for key, value in pairs]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([[], []], True),
        ([_kv(("a", "1"))], True),
        ([_kv(("a", "1"), ("b", "2")), _kv(("a", "x"))], True),
        ([_kv(("a", "1")), [], _kv(("a", "2"), ("é", "3"))], True),
        ([_kv(("b", "1"), ("a", "2"))], False),
        ([_kv(("a", "1"), ("a", "2"))], False),
        ([_kv(("a", None))], False),
        ([_kv((None, "1"))], False),
        ([None], False),
        ([_kv(("a", "1"), ("b", "2")), _kv(("c", "3"), ("b", "4"))], False),
    ],
)
def test_pairs_are_canonical(values: list[object], expected: bool) -> None:
    assert storage._pairs_are_canonical(_pairs(values)) is expected


def test_pairs_are_canonical_rejects_other_types() -> None:
    assert not storage._pairs_are_canonical(pa.array(["a"]))


@pytest.mark.parametrize(
    ("array", "expected"),
    [
        (pa.array([1.0, -2.0]), True),
        (pa.array([1.0, None]), False),
        (pa.array([1.0, math.nan]), False),
        (pa.array([1, 2]), False),
    ],
)
def test_bounds_are_plain(array: pa.Array, expected: bool) -> None:
    assert storage._bounds_are_plain(array) is expected


def test_fast_batch_declines_what_it_cannot_reproduce() -> None:
    batch = _arrow_batch(_records(2))
    assert storage._fast_batch(batch) is not None
    reordered = batch.select(list(reversed(SCHEMA.names)))
    assert storage._fast_batch(reordered) is None
    null_type = batch.set_column(
        SCHEMA.get_field_index("geometry_type"),
        "geometry_type",
        pa.array([None, "Polygon"], pa.string()),
    )
    assert storage._fast_batch(null_type) is None
    unsorted_tags = batch.set_column(
        SCHEMA.get_field_index("tags"),
        "tags",
        _pairs([[{"key": "b", "value": "1"}, {"key": "a", "value": "2"}], []]),
    )
    assert storage._fast_batch(unsorted_tags) is None
    nan_bbox = batch.set_column(
        SCHEMA.get_field_index("bbox_min_x"), "bbox_min_x", pa.array([math.nan, 0.0])
    )
    assert storage._fast_batch(nan_bbox) is None
    lossy_time = batch.set_column(
        SCHEMA.get_field_index("timestamp"),
        "timestamp",
        pa.array([1_500, 2_000], pa.timestamp("us", tz="UTC")),
    )
    assert storage._fast_batch(lossy_time) is None


def test_column_extreme_keeps_the_first_signed_zero() -> None:
    first_negative = storage._column_extreme(pa.array([-0.0, 0.0, 1.0]), pick_min=True)
    first_positive = storage._column_extreme(pa.array([0.0, -0.0, 1.0]), pick_min=True)
    assert math.copysign(1, first_negative) == -1
    assert math.copysign(1, first_positive) == 1
    assert storage._column_extreme(pa.array([-3.0, 2.0]), pick_min=True) == -3.0
    assert storage._column_extreme(pa.array([-3.0, 2.0]), pick_min=False) == 2.0
    assert math.copysign(1, storage._column_extreme(pa.array([-0.0, -1.0]), pick_min=False)) == -1


# --- stats: vectorized spatial summary -------------------------------------------------


def _spatial_batch(shapes: list[object], **overrides: object) -> pa.RecordBatch:
    columns: dict[str, object] = {
        "source_pbf": ["a.osm.pbf"] * len(shapes),
        "area_m2": [float(index + 1) for index in range(len(shapes))],
        "geometry_type": [getattr(shape, "geom_type", None) for shape in shapes],
        "bbox_min_x": [shape.bounds[0] for shape in shapes],  # type: ignore[attr-defined]
        "bbox_min_y": [shape.bounds[1] for shape in shapes],  # type: ignore[attr-defined]
        "bbox_max_x": [shape.bounds[2] for shape in shapes],  # type: ignore[attr-defined]
        "bbox_max_y": [shape.bounds[3] for shape in shapes],  # type: ignore[attr-defined]
        "geometry": [shape.wkb for shape in shapes],  # type: ignore[attr-defined]
    }
    columns.update(overrides)
    return pa.RecordBatch.from_pydict(columns)


def test_vectorized_spatial_summary_equals_the_row_pass() -> None:
    batch = _spatial_batch([_SQUARE, _HOLED, _MULTI, _SQUARE])
    fast = stats_module._vectorized_spatial_summary(batch)
    rows = stats_module._summarize_spatial_batch_rows(batch, source_name="a", row_offset=0)
    assert fast == rows
    assert stats_module._summarize_spatial_batch(batch, source_name="a", row_offset=0) == rows
    assert fast is not None
    assert fast.geometry_holes_total == 1
    assert fast.multipolygon_components_total == 2


def test_vectorized_spatial_summary_keeps_the_first_signed_zero() -> None:
    batch = _spatial_batch([_SQUARE, _SQUARE], bbox_min_x=[0.0, -0.0], bbox_max_x=[-0.0, 0.0])
    fast = stats_module._vectorized_spatial_summary(batch)
    rows = stats_module._summarize_spatial_batch_rows(batch, source_name="a", row_offset=0)
    assert fast is not None
    assert repr(fast.dataset_bbox) == repr(rows.dataset_bbox)


@pytest.mark.parametrize(
    "overrides",
    [
        {"area_m2": [1.0, None]},
        {"area_m2": [1, 2]},
        {"area_m2": [1.0, math.inf]},
        {"bbox_max_y": [1.0, math.nan]},
        {"geometry_type": ["Polygon", None]},
        {"geometry_type": ["Polygon", "LineString"]},
        {"geometry_type": ["Polygon", "MultiPolygon"]},
        {"geometry": [_SQUARE.wkb, None]},
        {"geometry": [_SQUARE.wkb, b"\x00"]},
        {"geometry": [_SQUARE.wkb, Polygon().wkb]},
        {"geometry": [_SQUARE.wkb, Polygon([(0, 0), (1, 1), (1, 0), (0, 1)]).wkb]},
        {"geometry": pa.array([_SQUARE.wkb, _SQUARE.wkb], pa.large_binary())},
    ],
)
def test_vectorized_spatial_summary_declines_invalid_batches(overrides: dict[str, object]) -> None:
    batch = _spatial_batch([_SQUARE, _SQUARE], **overrides)
    assert stats_module._vectorized_spatial_summary(batch) is None


def test_vectorized_spatial_summary_declines_an_empty_batch() -> None:
    batch = _spatial_batch([_SQUARE]).slice(0, 0)
    assert stats_module._vectorized_spatial_summary(batch) is None


def test_invalid_batches_still_report_the_row_pass_error() -> None:
    line = LineString([(0, 0), (1, 1)]).wkb
    batch = _spatial_batch([_SQUARE, _SQUARE], geometry=[_SQUARE.wkb, line])
    with pytest.raises(stats_module.ReportingError, match="at row 8"):
        stats_module._summarize_spatial_batch(batch, source_name="a", row_offset=7)


# --- H3 centroids ----------------------------------------------------------------------


def _centroid_batch(geometries: list[object]) -> pa.RecordBatch:
    return pa.RecordBatch.from_pydict(
        {
            "source_pbf": ["a.osm.pbf", "b.osm.pbf", "a.osm.pbf"][: len(geometries)],
            "osm_type": ["way"] * len(geometries),
            "osm_id": list(range(1, len(geometries) + 1)),
            "geometry": geometries,
        }
    )


def test_vectorized_centroids_equal_the_row_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = _centroid_batch([_SQUARE.wkb, _MULTI.wkb, _HOLED.wkb])
    paths = {"a.osm.pbf": Path("data/a.parquet")}
    fast = list(parquet_inputs._iter_centroid_batch(batch, paths))
    monkeypatch.setattr(parquet_inputs, "_vectorized_centroids", lambda _batch: None)
    rows = list(parquet_inputs._iter_centroid_batch(batch, paths))
    assert fast == rows
    assert fast[1][0] == Path("b.osm.pbf")


@pytest.mark.parametrize(
    "geometries",
    [
        [_SQUARE.wkb, None],
        [_SQUARE.wkb, b"\x00"],
        [_SQUARE.wkb, Polygon().wkb],
        [_SQUARE.wkb, Polygon([(0, 0), (1, 1), (1, 0), (0, 1)]).wkb],
        [_SQUARE.wkb, LineString([(0, 0), (1, 1)]).wkb],
        [_SQUARE.wkb, Polygon([(0, 95), (0, 96), (1, 96), (1, 95)]).wkb],
        [_SQUARE.wkb, Polygon([(185, 0), (185, 1), (186, 1), (186, 0)]).wkb],
        pa.array([_SQUARE.wkb], pa.large_binary()),
    ],
)
def test_vectorized_centroids_decline_invalid_batches(geometries: list[object]) -> None:
    assert parquet_inputs._vectorized_centroids(_centroid_batch(geometries)) is None


def test_centroid_batch_errors_come_from_the_row_pass() -> None:
    batch = _centroid_batch([_SQUARE.wkb, None])
    produced = parquet_inputs._iter_centroid_batch(batch, {})
    assert next(produced)[1:] == (0.5, 0.5)
    with pytest.raises(parquet_inputs.H3AggregationError, match="null geometry"):
        next(produced)


def test_empty_centroid_batch_yields_nothing() -> None:
    batch = _centroid_batch([_SQUARE.wkb]).slice(0, 0)
    assert list(parquet_inputs._iter_centroid_batch(batch, {})) == []


# --- area histogram --------------------------------------------------------------------


def _row_counts(values: list[object]) -> list[int]:
    counts = [0] * area_histogram.AREA_BUCKET_COUNT
    for value in values:
        if value is not None:
            counts[area_histogram._bucket_index(float(value))] += 1  # type: ignore[arg-type]
    return counts


@pytest.mark.parametrize(
    ("values", "data_type"),
    [
        (
            [
                *area_histogram.AREA_BUCKET_EDGES,
                *(math.nextafter(edge, -math.inf) for edge in area_histogram.AREA_BUCKET_EDGES),
                -1.0,
                -0.0,
                math.nan,
                math.inf,
                -math.inf,
                1e15,
                None,
            ],
            pa.float64(),
        ),
        ([0, 5, 12, None, 10**12], pa.int64()),
        ([], pa.float64()),
    ],
)
def test_bucket_counts_match_bisect(values: list[object], data_type: pa.DataType) -> None:
    counts = [1] * area_histogram.AREA_BUCKET_COUNT
    area_histogram._add_bucket_counts(counts, pa.array(values, type=data_type))
    assert counts == [count + 1 for count in _row_counts(values)]
