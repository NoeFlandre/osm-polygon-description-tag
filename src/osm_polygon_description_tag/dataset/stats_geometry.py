"""Geometry and extent aggregates for dataset statistics.

Unique rows are streamed one bounded Arrow batch at a time, so the dataset's WKB
is never retained in Python. A vectorized pass measures batches that are entirely
valid; any other batch goes through the per-row pass, which alone reports
malformed input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
import pyarrow as pa
import shapely
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from osm_polygon_description_tag.dataset.numeric import coerce_float_values as _coerce_float_values
from osm_polygon_description_tag.dataset.stats_manifest import ReportingError, ValidatedArtifact
from osm_polygon_description_tag.dataset.unique_rows import iter_unique_parquet_batches

_SPATIAL_COLUMNS = [
    "source_pbf",
    "osm_type",
    "osm_id",
    "geometry_type",
    "area_m2",
    "bbox_min_x",
    "bbox_min_y",
    "bbox_max_x",
    "bbox_max_y",
    "geometry",
]

_FAST_GEOMETRY_TYPE_IDS = {"Polygon": 3, "MultiPolygon": 6}
_BBOX_COLUMNS = ("bbox_min_x", "bbox_min_y", "bbox_max_x", "bbox_max_y")


@dataclass(frozen=True)
class _SpatialSummary:
    rows: int
    area_total_m2: float
    area_mean_m2: float | None
    dataset_bbox: tuple[float, float, float, float] | None
    geometry_vertices_total: int
    geometry_rings_total: int
    geometry_holes_total: int
    multipolygon_components_total: int


def _decode_geometry(
    wkb: bytes,
    expected_type: str,
    *,
    source_name: str,
    row_index: int,
) -> BaseGeometry:
    """Decode and validate one geometry value for reporting."""
    try:
        geometry = from_wkb(wkb)
    except (ValueError, ShapelyError) as error:
        raise ReportingError(
            f"malformed geometry in {source_name} at row {row_index}: {error}"
        ) from error
    if geometry.is_empty:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    if not geometry.is_valid:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    if geometry.geom_type != expected_type:
        raise ReportingError(
            f"invalid geometry in {source_name} at row {row_index}: "
            f"expected {expected_type!r}, got {geometry.geom_type!r}"
        )
    return geometry


def _polygon_components(
    geometry: BaseGeometry,
    *,
    source_name: str,
    row_index: int,
) -> tuple[tuple[Polygon, ...], int]:
    """Return polygon components and the MultiPolygon component count."""
    polygons: tuple[Polygon, ...]
    if isinstance(geometry, Polygon):
        return (geometry,), 0
    if isinstance(geometry, MultiPolygon):
        polygons = tuple(geometry.geoms)
        return polygons, len(polygons)
    # pragma: no cover - schema validation prevents this branch
    raise ReportingError(
        f"unsupported geometry in {source_name} at row {row_index}: {geometry.geom_type!r}"
    )


def _polygon_measurements(polygons: tuple[Polygon, ...]) -> tuple[int, int, int]:
    """Count unique vertices, rings, and holes across polygon components."""
    vertices = 0
    rings = 0
    holes = 0
    for polygon in polygons:
        all_rings = (polygon.exterior, *polygon.interiors)
        rings += len(all_rings)
        holes += len(polygon.interiors)
        vertices += sum(len(ring.coords) - 1 for ring in all_rings)
    return vertices, rings, holes


def _geometry_measurements(
    wkb: bytes,
    expected_type: str,
    *,
    source_name: str,
    row_index: int,
) -> tuple[int, int, int, int]:
    """Return vertex, ring, hole, and MultiPolygon-part totals for one row.

    A ring's closing coordinate is not counted twice as a vertex. Polygon
    components in a MultiPolygon are counted; a simple Polygon contributes
    zero to the MultiPolygon-part total.
    """
    geometry = _decode_geometry(
        wkb,
        expected_type,
        source_name=source_name,
        row_index=row_index,
    )
    polygons, multipolygon_components = _polygon_components(
        geometry,
        source_name=source_name,
        row_index=row_index,
    )
    vertices, rings, holes = _polygon_measurements(polygons)
    return vertices, rings, holes, multipolygon_components


def _validated_area(value: object, *, source_name: str, row_index: int) -> float:
    """Validate and normalize one persisted area value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReportingError(f"invalid area in {source_name} at row {row_index}")
    area = float(value)
    if not math.isfinite(area):
        raise ReportingError(f"invalid area in {source_name} at row {row_index}")
    return area


def _validated_geometry_type(value: object, *, source_name: str, row_index: int) -> str:
    """Validate one persisted geometry type value."""
    if not isinstance(value, str):
        raise ReportingError(f"missing geometry type in {source_name} at row {row_index}")
    return value


def _validated_bbox(
    values: tuple[object, ...],
    *,
    source_name: str,
    row_index: int,
) -> tuple[float, float, float, float]:
    """Validate one persisted bounding-box tuple."""
    coordinates = _coerce_bbox_values(values)
    if coordinates is None:
        raise ReportingError(f"invalid bounding box in {source_name} at row {row_index}")
    return coordinates[0], coordinates[1], coordinates[2], coordinates[3]


def _coerce_bbox_values(values: tuple[object, ...]) -> tuple[float, ...] | None:
    """Convert a bounding-box tuple and reject invalid coordinates."""
    coordinates = _coerce_float_values(values)
    if coordinates is None:
        return None
    if len(coordinates) != 4:
        return None
    if not all(map(math.isfinite, coordinates)):
        return None
    return coordinates


def _summarize_spatial_batch(
    batch: pa.RecordBatch,
    *,
    source_name: str,
    row_offset: int,
) -> _SpatialSummary:
    """Validate and summarize one bounded spatial Arrow batch.

    A vectorized pass handles batches that are entirely valid; any batch it
    cannot vouch for goes through the per-row pass, which alone reports errors.
    """
    summary = _vectorized_spatial_summary(batch)
    if summary is not None:
        return summary
    return _summarize_spatial_batch_rows(batch, source_name=source_name, row_offset=row_offset)


def _finite_float_column(batch: pa.RecordBatch, name: str) -> np.ndarray | None:
    column = batch.column(name)
    if column.type != pa.float64() or column.null_count:
        return None
    values = column.to_numpy()
    return values if bool(np.isfinite(values).all()) else None


def _first_extreme(values: np.ndarray, *, pick_min: bool) -> float:
    value = float(values.min() if pick_min else values.max())
    if value == 0.0:
        # Signed zeros compare equal; Python's min/max keep the first one seen.
        listed = values.tolist()
        return float(min(listed) if pick_min else max(listed))
    return value


def _expected_type_ids(batch: pa.RecordBatch) -> np.ndarray | None:
    geometry_types = batch.column("geometry_type")
    if geometry_types.null_count:
        return None
    type_names = geometry_types.to_pylist()
    if not set(type_names) <= _FAST_GEOMETRY_TYPE_IDS.keys():
        return None
    return np.array([_FAST_GEOMETRY_TYPE_IDS[name] for name in type_names])


def _decoded_wkbs(batch: pa.RecordBatch) -> np.ndarray | None:
    wkbs = batch.column("geometry")
    if wkbs.null_count or wkbs.type != pa.binary():
        return None
    try:
        # pragma: no mutate start - None and False both request the same non-zero-copy conversion
        return shapely.from_wkb(wkbs.to_numpy(zero_copy_only=False))
        # pragma: no mutate end
    except (ValueError, ShapelyError):
        return None


def _vectorized_geometries(batch: pa.RecordBatch) -> np.ndarray | None:
    """Decode the batch's geometry when every row would pass ``_decode_geometry``."""
    expected_ids = _expected_type_ids(batch)
    geometries = None if expected_ids is None else _decoded_wkbs(batch)
    if geometries is None:
        return None
    valid = (
        ~shapely.is_empty(geometries)
        & shapely.is_valid(geometries)
        & (shapely.get_type_id(geometries) == expected_ids)
    )
    return geometries if bool(valid.all()) else None


def _finite_bbox_columns(batch: pa.RecordBatch) -> list[np.ndarray] | None:
    columns = [_finite_float_column(batch, name) for name in _BBOX_COLUMNS]
    if any(values is None for values in columns):
        return None
    return cast(list[np.ndarray], columns)


def _vectorized_spatial_summary(batch: pa.RecordBatch) -> _SpatialSummary | None:
    """Summarize ``batch`` without per-row Python, or return ``None`` to fall back."""
    if batch.num_rows == 0:
        return None
    areas = _finite_float_column(batch, "area_m2")
    bboxes = _finite_bbox_columns(batch)
    if areas is None or bboxes is None:
        return None
    geometries = _vectorized_geometries(batch)
    if geometries is None:
        return None
    return _measured_summary(areas, bboxes, geometries)


def _measured_summary(
    areas: np.ndarray, bboxes: list[np.ndarray], geometries: np.ndarray
) -> _SpatialSummary:
    polygons = shapely.get_parts(geometries)
    holes = int(shapely.get_num_interior_rings(polygons).sum())
    rings = len(polygons) + holes
    multipolygons = shapely.get_type_id(geometries) == _FAST_GEOMETRY_TYPE_IDS["MultiPolygon"]
    min_x, min_y, max_x, max_y = (
        _first_extreme(values, pick_min=name.startswith("bbox_min"))
        for values, name in zip(bboxes, _BBOX_COLUMNS, strict=True)
    )
    return _SpatialSummary(
        rows=len(geometries),
        area_total_m2=math.fsum(areas.tolist()),
        area_mean_m2=None,
        dataset_bbox=(min_x, min_y, max_x, max_y),
        geometry_vertices_total=int(shapely.get_num_coordinates(polygons).sum()) - rings,
        geometry_rings_total=rings,
        geometry_holes_total=holes,
        multipolygon_components_total=int(
            shapely.get_num_geometries(geometries[multipolygons]).sum()
        ),
    )


def _summarize_spatial_batch_rows(
    batch: pa.RecordBatch,
    *,
    source_name: str,
    row_offset: int,
) -> _SpatialSummary:
    """Validate and summarize one spatial Arrow batch row by row."""
    areas = batch.column("area_m2").to_pylist()
    geometry_types = batch.column("geometry_type").to_pylist()
    min_x = batch.column("bbox_min_x").to_pylist()
    min_y = batch.column("bbox_min_y").to_pylist()
    max_x = batch.column("bbox_max_x").to_pylist()
    max_y = batch.column("bbox_max_y").to_pylist()
    # Columns of one Arrow batch are the same length by construction, so these
    # strict zips cannot fire. They stay as guards for a caller that assembles
    # the lists itself, and are excluded from mutation because no input can
    # tell them apart from a plain zip.
    bbox_values = zip(min_x, min_y, max_x, max_y, strict=True)  # pragma: no mutate
    geometries = batch.column("geometry").to_pylist()
    source_names = (
        batch.column("source_pbf").to_pylist()
        if "source_pbf" in batch.schema.names
        else [source_name] * len(areas)
    )
    area_values: list[float] = []
    bboxes: list[tuple[float, float, float, float]] = []
    vertices_total = 0
    rings_total = 0
    holes_total = 0
    multipolygon_components_total = 0
    # pragma: no mutate start - equal-length columns, see the note above
    rows = zip(areas, geometry_types, bbox_values, geometries, source_names, strict=True)
    # pragma: no mutate end
    for offset, (area, geometry_type, bbox, wkb, row_source) in enumerate(rows):
        index = row_offset + offset
        source = str(row_source)
        area_values.append(_validated_area(area, source_name=source, row_index=index))
        validated_type = _validated_geometry_type(
            geometry_type,
            source_name=source,
            row_index=index,
        )
        if not isinstance(wkb, bytes):
            raise ReportingError(f"missing geometry in {source} at row {index}")
        vertices, rings, holes, components = _geometry_measurements(
            wkb,
            validated_type,
            source_name=source,
            row_index=index,
        )
        bboxes.append(_validated_bbox(bbox, source_name=source, row_index=index))
        vertices_total += vertices
        rings_total += rings
        holes_total += holes
        multipolygon_components_total += components
    # every bbox is a 4-tuple from ``_validated_bbox``; see the note above
    columns = tuple(zip(*bboxes, strict=True))  # pragma: no mutate
    min_x = min(columns[0])
    min_y = min(columns[1])
    max_x = max(columns[2])
    max_y = max(columns[3])
    return _SpatialSummary(
        rows=len(area_values),
        area_total_m2=math.fsum(area_values),
        area_mean_m2=None,
        dataset_bbox=(min_x, min_y, max_x, max_y),
        geometry_vertices_total=vertices_total,
        geometry_rings_total=rings_total,
        geometry_holes_total=holes_total,
        multipolygon_components_total=multipolygon_components_total,
    )


def _merge_bboxes(
    current: tuple[float, float, float, float] | None,
    addition: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Merge dataset extents using min/min/max/max semantics."""
    if addition is None:
        return current
    if current is None:
        return addition
    return (
        min(current[0], addition[0]),
        min(current[1], addition[1]),
        max(current[2], addition[2]),
        max(current[3], addition[3]),
    )


def collect_spatial_summary(artifacts: tuple[ValidatedArtifact, ...]) -> _SpatialSummary:
    """Stream unique spatial rows and derive dataset-wide geometry facts."""
    if not artifacts:
        return _SpatialSummary(0, 0.0, None, None, 0, 0, 0, 0)
    data_root = artifacts[0].parquet.parent.parent
    # Areas are summed exactly at the end rather than accumulated batch by
    # batch: a running float total depends on the order the batches arrive in,
    # which made stats.json differ in its last bits between identical runs.
    batch_area_totals: list[float] = []
    row_count = 0
    dataset_bbox: tuple[float, float, float, float] | None = None
    vertices_total = 0
    rings_total = 0
    holes_total = 0
    multipolygon_components_total = 0

    row_index = 0
    for batch in iter_unique_parquet_batches(
        data_root,
        columns=_SPATIAL_COLUMNS,
        require_successful_text=True,
    ):
        summary = _summarize_spatial_batch(
            batch,
            source_name="unique.parquet",
            row_offset=row_index,
        )
        dataset_bbox = _merge_bboxes(dataset_bbox, summary.dataset_bbox)
        batch_area_totals.append(summary.area_total_m2)
        row_count += summary.rows
        row_index += summary.rows
        vertices_total += summary.geometry_vertices_total
        rings_total += summary.geometry_rings_total
        holes_total += summary.geometry_holes_total
        multipolygon_components_total += summary.multipolygon_components_total

    area_total = math.fsum(batch_area_totals)
    return _SpatialSummary(
        rows=row_count,
        area_total_m2=area_total,
        area_mean_m2=area_total / row_count if row_count else None,
        dataset_bbox=dataset_bbox,
        geometry_vertices_total=vertices_total,
        geometry_rings_total=rings_total,
        geometry_holes_total=holes_total,
        multipolygon_components_total=multipolygon_components_total,
    )
