from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest
from shapely.geometry import GeometryCollection, Point, Polygon

import osm_polygon_description_tag.dataset.geography.parquet_inputs as parquet_inputs
import osm_polygon_description_tag.dataset.stats as stats_module

_SQUARE = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])


def _spatial_batch(
    geometries: list[Polygon],
    areas: list[float],
    *,
    bbox_overrides: dict[str, object] | None = None,
) -> pa.RecordBatch:
    bounds = [geometry.bounds for geometry in geometries]
    columns: dict[str, object] = {
        "source_pbf": ["region.parquet"] * len(geometries),
        "geometry_type": [geometry.geom_type for geometry in geometries],
        "area_m2": pa.array(areas, type=pa.float64()),
        "bbox_min_x": pa.array([item[0] for item in bounds], type=pa.float64()),
        "bbox_min_y": pa.array([item[1] for item in bounds], type=pa.float64()),
        "bbox_max_x": pa.array([item[2] for item in bounds], type=pa.float64()),
        "bbox_max_y": pa.array([item[3] for item in bounds], type=pa.float64()),
        "geometry": [geometry.wkb for geometry in geometries],
    }
    for name, value in (bbox_overrides or {}).items():
        columns[name] = pa.array([value], type=pa.float64())
    return pa.RecordBatch.from_pydict(columns)


def test_centroid_batch_rejects_a_geometry_collection_with_polygon_members() -> None:
    collection = GeometryCollection([_SQUARE, Point(2, 2)])
    batch = pa.RecordBatch.from_pydict(
        {
            "source_pbf": ["region.osm.pbf"],
            "osm_type": ["way"],
            "osm_id": [42],
            "geometry": [collection.wkb],
        }
    )

    with pytest.raises(parquet_inputs.H3AggregationError, match="unsupported geometry type"):
        next(parquet_inputs._iter_centroid_batch(batch, {}))


@pytest.mark.parametrize(
    "column",
    [
        "bbox_min_x",
        "bbox_min_y",
        "bbox_max_x",
        "bbox_max_y",
    ],
)
@pytest.mark.parametrize("value", [None, float("nan")], ids=["null", "non-finite"])
def test_spatial_summary_reports_invalid_values_in_each_bbox_column(
    column: str,
    value: object,
) -> None:
    batch = _spatial_batch(
        [_SQUARE],
        [1.0],
        bbox_overrides={column: value},
    )

    with pytest.raises(
        stats_module.ReportingError,
        match=r"\Ainvalid bounding box in region\.parquet at row 7\Z",
    ):
        stats_module._summarize_spatial_batch(
            batch,
            source_name="caller.parquet",
            row_offset=7,
        )


def test_vectorized_spatial_summary_keeps_low_order_area_contributions() -> None:
    batch = _spatial_batch(
        [_SQUARE, _SQUARE, _SQUARE],
        [1e16, 1.0, 1.0],
    )

    summary = stats_module._summarize_spatial_batch(
        batch,
        source_name="region.parquet",
        row_offset=0,
    )

    assert summary.area_total_m2 == 10_000_000_000_000_002.0


def test_spatial_summary_uses_the_vectorized_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _spatial_batch([_SQUARE], [1.0])

    def row_fallback(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a valid batch must use the vectorized summary")

    monkeypatch.setattr(stats_module, "_summarize_spatial_batch_rows", row_fallback)

    summary = stats_module._summarize_spatial_batch(
        batch,
        source_name="region.parquet",
        row_offset=0,
    )

    assert summary is not None
    assert summary.rows == 1
    assert summary.area_total_m2 == 1.0


def test_measured_summary_rejects_extra_bbox_columns() -> None:
    batch = _spatial_batch([_SQUARE], [1.0])
    geometries = stats_module._vectorized_geometries(batch)
    assert geometries is not None

    with pytest.raises(ValueError, match=r"zip\(\).*shorter"):
        stats_module._measured_summary(
            np.array([1.0]),
            [np.array([0.0])] * 5,
            geometries,
        )
