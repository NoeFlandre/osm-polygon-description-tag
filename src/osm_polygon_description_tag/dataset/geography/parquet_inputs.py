"""Batched Parquet I/O for the H3 density aggregation.

This module owns the schema validation and the column-pruned
``iter_batches`` reads used by the H3 density aggregator. It does not
perform any rendering, aggregation, or aggregation policy. The full
dataset is never read into memory: the aggregator walks each Parquet
file via :meth:`ParquetFile.iter_batches` and yields one centroid per
row, keeping peak memory bounded by the batch size and by the number of
H3 cells observed, not the total number of rows.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry.base import BaseGeometry

from osm_polygon_description_tag.dataset.geography.h3_policy import (
    assign_h3_cell,
    validate_coordinate,
)

PARQUET_INPUT_COLUMNS: Final[tuple[str, ...]] = (
    "source_pbf",
    "osm_type",
    "osm_id",
    "geometry",
)
BATCH_SIZE: Final[int] = 4096


class H3AggregationError(RuntimeError):
    """Raised for invalid Parquet rows, malformed WKB, or invalid geometry."""


def sorted_parquets(directory: Path) -> list[Path]:
    """Return the deterministic sorted list of parquet files in ``directory``.

    This helper intentionally does NOT validate manifest matches; that
    step is the responsibility of
    :func:`osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts`,
    which is the shared validation primitive.
    """
    if not directory.exists():
        return []
    return sorted(directory.glob("*.parquet"))


def require_directory(path: Path, *, label: str) -> Path:
    """Return ``path`` after asserting it exists and is a directory.

    A missing directory is acceptable only when ``data_root`` is empty:
    the H3 aggregator then yields an empty count mapping. The
    :func:`aggregate_h3_density` entry point validates finalized
    artifacts (Parquet/manifest pairs) before streaming.
    """
    if not path.exists() or not path.is_dir():
        raise H3AggregationError(
            f"Required {label} directory does not exist: {path}. "
            "Run a complete PBF processing pass first."
        )
    return path


def _geometry_centroid(wkb: bytes | None) -> tuple[float, float] | None:
    """Return the Shapely (lon, lat) centroid of a WKB-encoded geometry.

    Returns ``None`` for ``None`` input. Raises :class:`H3AggregationError`
    for malformed WKB, invalid or empty geometry, or geometries that are
    not Polygon/MultiPolygon.
    """
    if wkb is None:
        return None
    geometry = _decode_geometry(wkb)
    _validate_geometry(geometry)
    point = geometry.centroid
    _validate_centroid(point)
    lon = float(point.x)
    lat = float(point.y)
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise H3AggregationError(f"non-finite centroid: lon={lon!r}, lat={lat!r}")
    return lon, lat


def _decode_geometry(wkb: bytes) -> BaseGeometry:
    try:
        geometry = from_wkb(wkb)
    except (ValueError, ShapelyError) as error:
        raise H3AggregationError(f"malformed WKB: {error}") from error
    return geometry


def _validate_geometry(geometry: BaseGeometry) -> None:
    if not isinstance(geometry, BaseGeometry):
        raise H3AggregationError(f"unsupported geometry type: {type(geometry).__name__}")
    if geometry.is_empty or not geometry.is_valid:
        raise H3AggregationError("invalid or empty geometry")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise H3AggregationError(
            f"unsupported geometry type for H3 density map: {geometry.geom_type!r}"
        )


def _validate_centroid(point: BaseGeometry) -> None:
    if point.is_empty or not point.is_valid:
        raise H3AggregationError("could not derive a finite centroid")


def iter_centroids(
    data_root: Path, *, batch_size: int = BATCH_SIZE
) -> Iterator[tuple[Path, float, float]]:
    """Yield ``(parquet_path, lon, lat)`` for every row in the dataset.

    The full dataset is never read into memory: each Parquet file is
    walked via :meth:`ParquetFile.iter_batches` with only the required
    columns. The iterator is deterministic when ``data_root`` contains a
    fixed sorted set of files.
    """
    data_dir = require_directory(data_root / "data", label="data")
    for parquet_path in sorted_parquets(data_dir):
        reader = pq.ParquetFile(parquet_path)
        for batch in reader.iter_batches(
            columns=list(PARQUET_INPUT_COLUMNS), batch_size=batch_size
        ):
            wkb_column = batch.column("geometry").to_pylist()
            osm_id_column = batch.column("osm_id").to_pylist()
            for index, wkb in enumerate(wkb_column):
                osm_id = osm_id_column[index]
                centroid = _geometry_centroid(wkb)
                if centroid is None:
                    raise H3AggregationError(f"null geometry at {parquet_path} (osm_id={osm_id!r})")
                lon, lat = centroid
                # Validate before H3 assignment for a clean error message.
                validate_coordinate(lat, lon)
                yield parquet_path, lon, lat


def collect_h3_counts(
    data_root: Path,
    *,
    h3_resolution: int | None = None,
) -> dict[str, int]:
    """Aggregate H3 cell counts from every validated Parquet in ``data_root``.

    Resolution handling uses an explicit ``None`` check: passing
    ``h3_resolution=0`` selects resolution 0 (valid), while passing
    ``h3_resolution=None`` falls back to the package default
    :data:`DEFAULT_H3_RESOLUTION`. The default itself is selected by
    leaving the parameter unset.

    The aggregated artifacts are first validated via
    :func:`osm_polygon_description_tag.dataset.storage.validate_finalized_artifacts`
    so that mismatched, stale, or corrupt parquet/manifest pairs cannot
    be observed as a partial map.
    """
    from osm_polygon_description_tag.dataset.geography.h3_policy import (
        DEFAULT_H3_RESOLUTION,
    )
    from osm_polygon_description_tag.dataset.storage import validate_finalized_artifacts

    validate_finalized_artifacts(data_root)

    counts: dict[str, int] = {}
    resolution = DEFAULT_H3_RESOLUTION if h3_resolution is None else h3_resolution
    for _path, lon, lat in iter_centroids(data_root):
        cell = assign_h3_cell(lat, lon, resolution=resolution)
        counts[cell] = counts.get(cell, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "BATCH_SIZE",
    "PARQUET_INPUT_COLUMNS",
    "H3AggregationError",
    "collect_h3_counts",
    "iter_centroids",
    "require_directory",
    "sorted_parquets",
]
