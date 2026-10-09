"""Bounded GeoParquet validation.

The uniqueness check uses a temporary SQLite primary-key table to keep memory
usage flat regardless of row count.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry.base import BaseGeometry

from osm_polygon_description_tag.dataset.schema import (
    KEY_VALUE_COLUMNS,
    SCHEMA,
)
from osm_polygon_description_tag.dataset.storage_errors import StorageError
from osm_polygon_description_tag.dataset.text import (
    has_successful_description_text,
    is_nonempty_text,
    is_trimmed_nonempty_text,
)

_VALID_GEOMETRY_TYPES = {"Polygon", "MultiPolygon"}


_VALIDATION_COLUMNS = [
    "source_pbf",
    "osm_type",
    "osm_id",
    "description",
    "localized_descriptions",
    "geometry_type",
    "area_m2",
    "bbox_min_x",
    "bbox_min_y",
    "bbox_max_x",
    "bbox_max_y",
    "geometry",
]


def _check_schema(file_schema: pa.Schema) -> None:
    if file_schema.names != SCHEMA.names:
        raise StorageError(f"schema field names mismatch: {file_schema.names}")
    for name in SCHEMA.names:
        _check_field(file_schema.field(name), name)


def _check_field(actual: pa.Field, name: str) -> None:
    expected = SCHEMA.field(name)
    legacy_map = name in KEY_VALUE_COLUMNS and actual.type == pa.map_(pa.string(), pa.string())
    if actual.type != expected.type and not legacy_map:
        raise StorageError(f"field type mismatch for {name}: {actual.type} != {expected.type}")
    if actual.nullable != expected.nullable:
        raise StorageError(f"field nullability mismatch for {name}")


def _read_geo_metadata(schema: pa.Schema) -> dict[str, Any]:
    raw = (schema.metadata or {}).get(b"geo")
    if raw is None:
        raise StorageError("missing GeoParquet 'geo' metadata")
    try:
        geo = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StorageError(f"invalid GeoParquet 'geo' metadata: {error}") from error
    _validate_geo_metadata_header(geo)
    column = _geometry_metadata_column(geo)
    _validate_geometry_metadata_column(column)
    return geo


def _validate_geo_metadata_header(geo: Mapping[str, Any]) -> None:
    if geo.get("version") != "1.1.0":
        raise StorageError(f"unsupported GeoParquet version: {geo.get('version')!r}")
    if geo.get("primary_column") != "geometry":
        raise StorageError("primary geometry column must be 'geometry'")


def _geometry_metadata_column(geo: Mapping[str, Any]) -> dict[str, Any]:
    columns = geo.get("columns")
    if not isinstance(columns, dict) or "geometry" not in columns:
        raise StorageError("missing 'geometry' column metadata")
    column = columns["geometry"]
    if not isinstance(column, dict):
        raise StorageError("geometry metadata must be an object")
    return column


def _validate_geometry_metadata_column(column: Mapping[str, Any]) -> None:
    if column.get("encoding") != "WKB":
        raise StorageError("geometry encoding must be WKB")


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
        # pragma: no mutate start - SQL keyword and identifier case are equivalent
        self._connection.execute(
            "CREATE TABLE pk (osm_type TEXT NOT NULL, osm_id INTEGER NOT NULL, "
            "PRIMARY KEY (osm_type, osm_id))"
        )
        # pragma: no mutate end
        self._closed = False  # pragma: no mutate - None has the same runtime state

    def check_and_add(self, osm_type: str, osm_id: int) -> None:
        try:
            # pragma: no mutate start - SQL keyword and identifier case are equivalent
            self._connection.execute(
                "INSERT INTO pk (osm_type, osm_id) VALUES (?, ?)", (osm_type, osm_id)
            )
            # pragma: no mutate end
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


@dataclass
class _ValidationState:
    uniqueness: _UniquenessIndex
    actual_types: set[str] = field(default_factory=set)
    source_pbf: str | None = None
    min_x: float = math.inf
    min_y: float = math.inf
    max_x: float = -math.inf
    max_y: float = -math.inf
    row_count: int = 0


def _batch_columns(batch: pa.RecordBatch) -> dict[str, list[Any]]:
    return {name: batch.column(name).to_pylist() for name in _VALIDATION_COLUMNS}


def _validate_source(
    state: _ValidationState, current: str, expected_source_pbf: str | None = None
) -> None:
    if state.source_pbf is None:
        if expected_source_pbf is not None and current != expected_source_pbf:
            raise StorageError(
                "manifest source identity mismatch: "
                f"expected {expected_source_pbf!r}, found {current!r}"
            )
        state.source_pbf = current
    elif state.source_pbf != current:
        raise StorageError(f"mixed source_pbf within file: {state.source_pbf!r} and {current!r}")


def _validate_geometry_type(state: _ValidationState, geometry_type: str) -> None:
    if geometry_type not in _VALID_GEOMETRY_TYPES:
        raise StorageError(f"unsupported geometry_type: {geometry_type!r}")
    state.actual_types.add(geometry_type)


def _validate_successful_text(
    value: str,
    *,
    empty_message: str,
    trimmed_message: str,
) -> None:
    if not is_nonempty_text(value):
        raise StorageError(empty_message)
    if not is_trimmed_nonempty_text(value):
        raise StorageError(trimmed_message)


def _validate_base_description(
    description: object,
    *,
    require_successful_text: bool = True,
) -> None:
    if description is None:
        return
    if not isinstance(description, str):
        raise StorageError("description must be a string")
    if require_successful_text:
        _validate_successful_text(
            description,
            empty_message="description text must be non-empty",
            trimmed_message="description text must be trimmed",
        )


def _localized_description_entries(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise StorageError("localized descriptions must be a sequence")
    return cast(Sequence[object], value)


def _validate_localized_key(key: object, seen_keys: set[str]) -> str:
    if not isinstance(key, str):
        raise StorageError("localized description key is malformed")
    if key in seen_keys:
        raise StorageError(f"duplicate localized description key: {key!r}")
    return key


def _validate_localized_value(
    value: object,
    *,
    require_successful_text: bool = True,
) -> None:
    if value is None and not require_successful_text:
        return
    if not isinstance(value, str):
        raise StorageError("localized description value must be non-empty text")
    if require_successful_text:
        _validate_successful_text(
            value,
            empty_message="localized description value must be non-empty text",
            trimmed_message="localized description value must be trimmed",
        )


def _validate_localized_entry(
    entry: object,
    seen_keys: set[str],
    *,
    require_successful_text: bool = True,
) -> None:
    if not isinstance(entry, Mapping):
        raise StorageError("localized description entry is malformed")
    key = _validate_localized_key(entry.get("key"), seen_keys)
    _validate_localized_value(
        entry.get("value"),
        require_successful_text=require_successful_text,
    )
    seen_keys.add(key)


def _validate_localized_descriptions(
    value: object,
    *,
    require_successful_text: bool = True,
) -> int:
    entries = _localized_description_entries(value)
    seen_keys: set[str] = set()
    for entry in entries:
        _validate_localized_entry(
            entry,
            seen_keys,
            require_successful_text=require_successful_text,
        )
    return len(seen_keys)


def _validate_description_values(
    description: object,
    localized_descriptions: object,
    *,
    require_successful_text: bool = True,
) -> None:
    """Enforce the final-artifact successful non-empty-text invariant."""
    _validate_base_description(description, require_successful_text=require_successful_text)
    _validate_localized_descriptions(
        localized_descriptions,
        require_successful_text=require_successful_text,
    )

    if require_successful_text and not has_successful_description_text(
        description,
        localized_descriptions,
    ):
        raise StorageError("at least one non-empty description text is required")


def _validate_area(area: float | None) -> None:
    if area is None or not math.isfinite(area) or area <= 0:
        raise StorageError(f"non-positive or non-finite area_m2: {area}")


def _validate_bbox(
    state: _ValidationState,
    values: tuple[float | None, float | None, float | None, float | None],
) -> None:
    min_x, min_y, max_x, max_y = tuple(
        _validate_coordinate(value, label)
        for value, label in zip(
            values,
            ("bbox_min_x", "bbox_min_y", "bbox_max_x", "bbox_max_y"),
            strict=True,  # pragma: no mutate - values and labels are fixed four-tuples
        )
    )
    if min_x > max_x or min_y > max_y:
        raise StorageError("bbox min coordinate exceeds max")
    state.min_x = min(state.min_x, min_x)
    state.min_y = min(state.min_y, min_y)
    state.max_x = max(state.max_x, max_x)
    state.max_y = max(state.max_y, max_y)


def _validate_coordinate(value: float | None, label: str) -> float:
    if value is None or not math.isfinite(value):
        raise StorageError(f"non-finite {label}: {value}")
    return value


def _validate_geometry(geometry: bytes | None, geometry_type: str) -> None:
    if geometry is None:
        raise StorageError("null geometry")
    decoded = _decode_geometry(geometry)
    if decoded.geom_type != geometry_type or not decoded.is_valid or decoded.is_empty:
        raise StorageError(f"geometry_type/WKB mismatch or invalid geometry: {decoded.geom_type}")


def _decode_geometry(geometry: bytes) -> BaseGeometry:
    try:
        return from_wkb(geometry)
    except ShapelyError as error:
        raise StorageError(f"undecodable WKB geometry: {error}") from error


def _validate_row(
    columns: Mapping[str, list[Any]],
    index: int,
    state: _ValidationState,
    *,
    require_successful_text: bool = True,
    expected_source_pbf: str | None = None,
) -> None:
    osm_type = columns["osm_type"][index]
    osm_id = columns["osm_id"][index]
    state.uniqueness.check_and_add(osm_type, osm_id)
    _validate_source(state, columns["source_pbf"][index], expected_source_pbf)
    _validate_description_values(
        columns["description"][index],
        columns["localized_descriptions"][index],
        require_successful_text=require_successful_text,
    )
    geometry_type = columns["geometry_type"][index]
    _validate_geometry_type(state, geometry_type)
    _validate_area(columns["area_m2"][index])
    _validate_bbox(
        state,
        (
            columns["bbox_min_x"][index],
            columns["bbox_min_y"][index],
            columns["bbox_max_x"][index],
            columns["bbox_max_y"][index],
        ),
    )
    _validate_geometry(columns["geometry"][index], geometry_type)
    state.row_count += 1


def _validate_batch(
    batch: pa.RecordBatch,
    state: _ValidationState,
    *,
    require_successful_text: bool = True,
    expected_source_pbf: str | None = None,
) -> None:
    columns = _batch_columns(batch)
    for index in range(batch.num_rows):
        _validate_row(
            columns,
            index,
            state,
            require_successful_text=require_successful_text,
            expected_source_pbf=expected_source_pbf,
        )


def _validate_metadata_extent(
    state: _ValidationState,
    meta_types: set[str],
    meta_bbox: object,
) -> None:
    if state.actual_types != meta_types:
        raise StorageError(
            "geometry_types mismatch: actual "
            f"{sorted(state.actual_types)} != metadata {sorted(meta_types)}"
        )
    if state.row_count == 0 or meta_bbox is None:
        return
    _validate_metadata_bbox(state, meta_bbox)


def _validate_metadata_bbox(state: _ValidationState, meta_bbox: object) -> None:
    actual_bbox = [state.min_x, state.min_y, state.max_x, state.max_y]
    expected_bbox = cast(list[float], meta_bbox)
    for actual, expected in zip(actual_bbox, expected_bbox, strict=True):
        if abs(actual - expected) > 1e-9:
            raise StorageError(f"bbox mismatch: actual {actual_bbox} != metadata {expected_bbox}")


def validate_geoparquet(
    path: Path,
    *,
    require_successful_text: bool = True,
    expected_source_pbf: str | None = None,
) -> int:
    """Validate a GeoParquet file in batches and return its row count.

    Strict validation requires every persisted row to contain successful,
    trimmed, non-empty description text. Reporting and metadata-only release
    paths may set ``require_successful_text=False`` for legacy artifacts: the
    file's schema, geometry, identity, and text value types remain validated,
    while the shared SQL population predicate excludes rejected text rows.
    """
    if not path.is_file():
        raise StorageError(f"missing parquet: {path}")
    try:
        pf = pq.ParquetFile(path)
        _check_schema(pf.schema_arrow)
        geo = _read_geo_metadata(pf.schema_arrow)
        column_meta = geo["columns"]["geometry"]
        meta_types = set(column_meta.get("geometry_types", []))
        meta_bbox = column_meta.get("bbox")
        data_root = path.parent.parent
        state = _ValidationState(
            uniqueness=_UniquenessIndex(work_root=data_root / ".work" / "validation")
        )
        try:
            for batch in pf.iter_batches(columns=_VALIDATION_COLUMNS):
                _validate_batch(
                    batch,
                    state,
                    require_successful_text=require_successful_text,
                    expected_source_pbf=expected_source_pbf,
                )
        finally:
            state.uniqueness.close()
        _validate_metadata_extent(state, meta_types, meta_bbox)
        return state.row_count
    except (OSError, pa.ArrowException) as error:
        raise StorageError(f"cannot read GeoParquet {path}: {error}") from error


__all__ = ["validate_geoparquet"]
