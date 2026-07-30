"""Atomic GeoParquet 1.1 writing and bounded artifact validation.

A file is promoted only after streaming into an owned temporary, rewriting once
with final GeoParquet metadata (known only after the full pass), and passing
validation. Temporary files are confined to the output side and cleaned up
exactly. The rewrite pass streams batches through :meth:`ParquetFile.iter_batches`
so the per-PBF Parquet never loads entirely into memory. The bounded uniqueness
check uses a temporary SQLite primary-key table to keep memory usage flat
regardless of row count.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.errors import ShapelyError

from osm_polygon_description_tag.dataset.schema import SCHEMA, geo_metadata

_DICTIONARY_COLUMNS = ["source_pbf", "osm_type", "geometry_type"]
_VALID_GEOMETRY_TYPES = {"Polygon", "MultiPolygon"}
_VALIDATION_COLUMNS = [
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


class StorageError(ValueError):
    """Raised for integrity or infrastructure failures during storage."""


def _owned_temp(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def _fsync_path(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stream_rewrite_with_metadata(
    source_parquet: Path,
    target: Path,
    *,
    geometry_types: list[str],
    bbox: list[float],
) -> None:
    """Rewrite ``source_parquet`` into ``target`` with final GeoParquet metadata.

    Streams :meth:`ParquetFile.iter_batches` into the destination writer so
    the source table is never loaded into memory.
    """
    metadata = geo_metadata(geometry_types, bbox)
    schema_geo = SCHEMA.with_metadata({b"geo": json.dumps(metadata).encode("utf-8")})
    reader = pq.ParquetFile(source_parquet)
    with pq.ParquetWriter(
        target,
        schema_geo,
        compression="zstd",
        use_dictionary=_DICTIONARY_COLUMNS,
    ) as writer:
        for batch in reader.iter_batches(batch_size=4096):
            writer.write_batch(batch)


def write_geoparquet(
    records: Iterable[dict[str, object]],
    target: Path,
    *,
    batch_size: int = 1024,
    validator: Callable[[Path], int] | None = None,
) -> int:
    """Stream ``records`` into a validated GeoParquet file atomically promoted to ``target``."""
    if validator is None:
        validator = validate_geoparquet
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_data = _owned_temp(target)
    temp_final = _owned_temp(target)
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    geometry_types: set[str] = set()
    row_count = 0
    try:
        with pq.ParquetWriter(
            temp_data,
            SCHEMA,
            compression="zstd",
            use_dictionary=_DICTIONARY_COLUMNS,
        ) as writer:
            batch: list[dict[str, object]] = []
            for record in records:
                batch.append(record)
                row_count += 1
                geometry_types.add(str(record["geometry_type"]))
                min_x = min(min_x, float(cast(float, record["bbox_min_x"])))
                min_y = min(min_y, float(cast(float, record["bbox_min_y"])))
                max_x = max(max_x, float(cast(float, record["bbox_max_x"])))
                max_y = max(max_y, float(cast(float, record["bbox_max_y"])))
                if len(batch) >= batch_size:
                    writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=SCHEMA))
                    batch.clear()
            if batch:
                writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=SCHEMA))
            if row_count == 0:
                writer.write_batch(pa.RecordBatch.from_pylist([], schema=SCHEMA))

        bbox = [min_x, min_y, max_x, max_y] if row_count else []
        _stream_rewrite_with_metadata(
            temp_data, temp_final, geometry_types=sorted(geometry_types), bbox=bbox
        )

        validated_rows = validator(temp_final)
        _fsync_path(temp_final)
        _fsync_dir(target.parent)
        os.replace(temp_final, target)
        return validated_rows
    finally:
        for temp in (temp_data, temp_final):
            if temp.exists():
                temp.unlink()


def _check_schema(file_schema: pa.Schema) -> None:
    if file_schema.names != SCHEMA.names:
        raise StorageError(f"schema field names mismatch: {file_schema.names}")
    for name in SCHEMA.names:
        expected = SCHEMA.field(name)
        actual = file_schema.field(name)
        if actual.type != expected.type:
            raise StorageError(f"field type mismatch for {name}: {actual.type} != {expected.type}")
        if actual.nullable != expected.nullable:
            raise StorageError(f"field nullability mismatch for {name}")


def _read_geo_metadata(schema: pa.Schema) -> dict[str, Any]:
    metadata = schema.metadata or {}
    raw = metadata.get(b"geo")
    if raw is None:
        raise StorageError("missing GeoParquet 'geo' metadata")
    try:
        geo = cast(dict[str, Any], json.loads(raw))
    except json.JSONDecodeError as error:
        raise StorageError(f"invalid GeoParquet 'geo' metadata: {error}") from error
    if geo.get("version") != "1.1.0":
        raise StorageError(f"unsupported GeoParquet version: {geo.get('version')!r}")
    if geo.get("primary_column") != "geometry":
        raise StorageError("primary geometry column must be 'geometry'")
    columns = geo.get("columns")
    if not isinstance(columns, dict) or "geometry" not in columns:
        raise StorageError("missing 'geometry' column metadata")
    column = columns["geometry"]
    if not isinstance(column, dict) or column.get("encoding") != "WKB":
        raise StorageError("geometry encoding must be WKB")
    return geo


class _UniquenessIndex:
    """Disk-backed primary-key uniqueness check backed by an owned SQLite file.

    The SQLite database is created in an explicitly owned temporary directory
    (``<temp>/.osm-validate-<uuid>/uniqueness.db``) so the OS reclaims the
    file when the process exits and so memory stays bounded regardless of
    row count.
    """

    def __init__(self, *, work_root: Path) -> None:
        self._work_root = work_root
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._temp_dir = self._work_root / f".osm-validate-{uuid.uuid4().hex}"
        self._temp_dir.mkdir()
        self.db_path = self._temp_dir / "uniqueness.db"
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.execute(
            "CREATE TABLE pk (osm_type TEXT NOT NULL, osm_id INTEGER NOT NULL, "
            "PRIMARY KEY (osm_type, osm_id))"
        )
        self._closed = False

    def check_and_add(self, osm_type: str, osm_id: int) -> None:
        try:
            self._connection.execute(
                "INSERT INTO pk (osm_type, osm_id) VALUES (?, ?)", (osm_type, osm_id)
            )
        except sqlite3.IntegrityError as error:
            raise StorageError(f"duplicate (osm_type, osm_id): ({osm_type!r}, {osm_id})") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            import shutil

            shutil.rmtree(self._temp_dir, ignore_errors=True)
            with contextlib.suppress(OSError):
                self._work_root.rmdir()

    def __enter__(self) -> _UniquenessIndex:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def validate_geoparquet(path: Path) -> int:
    """Validate a finalized GeoParquet file in batches and return its row count."""
    if not path.is_file():
        raise StorageError(f"missing parquet: {path}")
    pf = pq.ParquetFile(path)
    _check_schema(pf.schema_arrow)
    geo = _read_geo_metadata(pf.schema_arrow)
    column_meta = cast(dict[str, Any], cast(dict[str, Any], geo["columns"])["geometry"])
    meta_types = set(cast(list[str], column_meta.get("geometry_types", [])))
    meta_bbox = column_meta.get("bbox")

    data_root = path.parent.parent
    uniqueness = _UniquenessIndex(work_root=data_root / ".work" / "validation")
    actual_types: set[str] = set()
    source_pbf: str | None = None
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    row_count = 0

    try:
        for batch in pf.iter_batches(columns=_VALIDATION_COLUMNS):
            osm_types = cast(list[str], batch.column("osm_type").to_pylist())
            osm_ids = cast(list[int], batch.column("osm_id").to_pylist())
            gtypes = cast(list[str], batch.column("geometry_type").to_pylist())
            areas = cast(list[float], batch.column("area_m2").to_pylist())
            bx_min = cast(list[float], batch.column("bbox_min_x").to_pylist())
            by_min = cast(list[float], batch.column("bbox_min_y").to_pylist())
            bx_max = cast(list[float], batch.column("bbox_max_x").to_pylist())
            by_max = cast(list[float], batch.column("bbox_max_y").to_pylist())
            geoms = cast(list[bytes], batch.column("geometry").to_pylist())
            sources = cast(list[str], batch.column("source_pbf").to_pylist())

            for index in range(batch.num_rows):
                row_count += 1
                uniqueness.check_and_add(osm_types[index], osm_ids[index])

                current_source = sources[index]
                if source_pbf is None:
                    source_pbf = current_source
                elif source_pbf != current_source:
                    raise StorageError(
                        f"mixed source_pbf within file: {source_pbf!r} and {current_source!r}"
                    )

                gtype = gtypes[index]
                if gtype not in _VALID_GEOMETRY_TYPES:
                    raise StorageError(f"unsupported geometry_type: {gtype!r}")
                actual_types.add(gtype)

                area = areas[index]
                if area is None or not math.isfinite(area) or area <= 0:
                    raise StorageError(f"non-positive or non-finite area_m2: {area}")

                for value, label in (
                    (bx_min[index], "bbox_min_x"),
                    (by_min[index], "bbox_min_y"),
                    (bx_max[index], "bbox_max_x"),
                    (by_max[index], "bbox_max_y"),
                ):
                    if value is None or not math.isfinite(value):
                        raise StorageError(f"non-finite {label}: {value}")
                if bx_min[index] > bx_max[index] or by_min[index] > by_max[index]:
                    raise StorageError("bbox min coordinate exceeds max")
                min_x = min(min_x, bx_min[index])
                min_y = min(min_y, by_min[index])
                max_x = max(max_x, bx_max[index])
                max_y = max(max_y, by_max[index])

                geom = geoms[index]
                if geom is None:
                    raise StorageError("null geometry")
                try:
                    decoded = from_wkb(geom)
                except ShapelyError as error:
                    raise StorageError(f"undecodable WKB geometry: {error}") from error
                if decoded.geom_type != gtype or not decoded.is_valid or decoded.is_empty:
                    raise StorageError(
                        f"geometry_type/WKB mismatch or invalid geometry: {decoded.geom_type}"
                    )
    finally:
        uniqueness.close()

    if actual_types != meta_types:
        raise StorageError(
            "geometry_types mismatch: actual "
            f"{sorted(actual_types)} != metadata {sorted(meta_types)}"
        )
    if row_count > 0 and meta_bbox is not None:
        actual_bbox = [min_x, min_y, max_x, max_y]
        expected_bbox = cast(list[float], meta_bbox)
        for actual, expected in zip(actual_bbox, expected_bbox, strict=True):
            if abs(actual - expected) > 1e-9:
                raise StorageError(
                    f"bbox mismatch: actual {actual_bbox} != metadata {expected_bbox}"
                )
    return row_count


__all__ = [
    "StorageError",
    "validate_geoparquet",
    "write_geoparquet",
]
