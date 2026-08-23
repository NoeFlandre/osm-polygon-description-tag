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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.errors import ShapelyError

from osm_polygon_description_tag.dataset.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    output_identity_for,
    read_manifest,
)
from osm_polygon_description_tag.dataset.schema import (
    KEY_VALUE_COLUMNS,
    SCHEMA,
    geo_metadata,
    mapping_to_pairs,
)

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


@dataclass(frozen=True)
class _RecordStreamSummary:
    row_count: int
    geometry_types: frozenset[str]
    bbox: tuple[float, float, float, float] | None


def _arrow_record(record: Mapping[str, object]) -> dict[str, object]:
    """Convert transform-layer mappings to the Hub-compatible Arrow shape."""
    normalized = dict(record)
    for column in KEY_VALUE_COLUMNS:
        normalized[column] = mapping_to_pairs(record.get(column))
    return normalized


def _owned_temp(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def _fsync_path(path: Path) -> None:
    with open(path, "rb") as handle:  # pragma: no mutate - mode does not affect fsync
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
    schema_geo = SCHEMA.with_metadata({b"geo": json.dumps(metadata).encode()})
    reader = pq.ParquetFile(source_parquet)
    with pq.ParquetWriter(
        target,
        schema_geo,
        compression="zstd",
        use_dictionary=_DICTIONARY_COLUMNS,
    ) as writer:
        for batch in reader.iter_batches(batch_size=4096):
            writer.write_batch(batch)


def _record_bounds(record: Mapping[str, object]) -> tuple[float, float, float, float]:
    # pragma: no mutate start - static narrowing only
    return (
        float(cast(float, record["bbox_min_x"])),
        float(cast(float, record["bbox_min_y"])),
        float(cast(float, record["bbox_max_x"])),
        float(cast(float, record["bbox_max_y"])),
    )
    # pragma: no mutate end


def _merge_bounds(
    current: tuple[float, float, float, float] | None,
    update: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if current is None:
        return update
    return (
        min(current[0], update[0]),
        min(current[1], update[1]),
        max(current[2], update[2]),
        max(current[3], update[3]),
    )


def _stream_records(
    records: Iterable[dict[str, object]],
    writer: pq.ParquetWriter,
    batch_size: int,
) -> _RecordStreamSummary:
    batch: list[dict[str, object]] = []
    geometry_types: set[str] = set()
    bounds: tuple[float, float, float, float] | None = None
    row_count = 0
    for record in records:
        batch.append(_arrow_record(record))
        row_count += 1
        geometry_types.add(str(record["geometry_type"]))
        bounds = _merge_bounds(bounds, _record_bounds(record))
        if len(batch) >= batch_size:
            writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=SCHEMA))
            batch.clear()
    if batch:
        writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=SCHEMA))
    if row_count == 0:
        writer.write_batch(pa.RecordBatch.from_pylist([], schema=SCHEMA))
    return _RecordStreamSummary(row_count, frozenset(geometry_types), bounds)


def _require_batch_size(batch_size: int) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")


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
    _require_batch_size(batch_size)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_data = _owned_temp(target)
    temp_final = _owned_temp(target)
    try:
        with pq.ParquetWriter(
            temp_data,
            SCHEMA,
            compression="zstd",
            use_dictionary=_DICTIONARY_COLUMNS,
        ) as writer:
            summary = _stream_records(records, writer, batch_size)

        bbox = list(summary.bbox) if summary.bbox is not None else []
        _stream_rewrite_with_metadata(
            temp_data,
            temp_final,
            geometry_types=sorted(summary.geometry_types),
            bbox=bbox,
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


def _validate_source(state: _ValidationState, current: str) -> None:
    if state.source_pbf is None:
        state.source_pbf = current
    elif state.source_pbf != current:
        raise StorageError(f"mixed source_pbf within file: {state.source_pbf!r} and {current!r}")


def _validate_geometry_type(state: _ValidationState, geometry_type: str) -> None:
    if geometry_type not in _VALID_GEOMETRY_TYPES:
        raise StorageError(f"unsupported geometry_type: {geometry_type!r}")
    state.actual_types.add(geometry_type)


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


def _decode_geometry(geometry: bytes):
    try:
        return from_wkb(geometry)
    except ShapelyError as error:
        raise StorageError(f"undecodable WKB geometry: {error}") from error


def _validate_row(columns: Mapping[str, list[Any]], index: int, state: _ValidationState) -> None:
    osm_type = columns["osm_type"][index]
    osm_id = columns["osm_id"][index]
    state.uniqueness.check_and_add(osm_type, osm_id)
    _validate_source(state, columns["source_pbf"][index])
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


def _validate_batch(batch: pa.RecordBatch, state: _ValidationState) -> None:
    columns = _batch_columns(batch)
    for index in range(batch.num_rows):
        _validate_row(columns, index, state)


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
    expected_bbox = cast(list[float], meta_bbox)  # pragma: no mutate - static narrowing only
    for actual, expected in zip(actual_bbox, expected_bbox, strict=True):
        if abs(actual - expected) > 1e-9:
            raise StorageError(f"bbox mismatch: actual {actual_bbox} != metadata {expected_bbox}")


def validate_geoparquet(path: Path) -> int:
    """Validate a finalized GeoParquet file in batches and return its row count."""
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
                _validate_batch(batch, state)
        finally:
            state.uniqueness.close()
        _validate_metadata_extent(state, meta_types, meta_bbox)
        return state.row_count
    except (OSError, pa.ArrowException) as error:
        raise StorageError(f"cannot read GeoParquet {path}: {error}") from error


def validate_finalized_artifacts(data_root):
    """Validate every finalized Parquet/manifest pair under data_root.

    The validation is intentionally minimal: it only checks that every
    Parquet has a matching, parseable, schema-current manifest whose
    output identity matches the Parquet. It does NOT call
    :func:`validate_geoparquet`; that stricter byte-level validation is
    performed separately, downstream, when the artifact is loaded.
    """
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_artifact_directories(data_dir, manifests_dir)

    parquets = sorted(
        data_dir.glob("*.parquet"), key=lambda p: p.name
    )  # pragma: no mutate - all paths share one parent
    manifest_paths = sorted(
        manifests_dir.glob("*.manifest.json"), key=lambda p: p.name
    )  # pragma: no mutate - all paths share one parent
    _check_artifact_stems(parquets, manifest_paths)

    validated_manifests = [_validate_manifest_pair(parquet, manifests_dir) for parquet in parquets]

    return {
        "parquets": tuple(parquets),
        "manifests": tuple(validated_manifests),
    }


def _require_artifact_directories(data_dir: Path, manifests_dir: Path) -> None:
    if not data_dir.is_dir():
        raise StorageError(f"missing data directory: {data_dir}")
    if not manifests_dir.is_dir():
        raise StorageError(f"missing manifests directory: {manifests_dir}")


def _check_artifact_stems(parquets: list[Path], manifests: list[Path]) -> None:
    parquet_stems = {p.name.removesuffix(".parquet") for p in parquets}
    manifest_stems = {p.name.removesuffix(".manifest.json") for p in manifests}
    mismatch = parquet_stems.symmetric_difference(manifest_stems)
    if mismatch:
        raise StorageError(f"artifact/manifest mismatch (missing or extra): {sorted(mismatch)}")


def _validate_manifest_pair(parquet: Path, manifests_dir: Path) -> Path:
    stem = parquet.name.removesuffix(".parquet")
    manifest_path = manifests_dir / f"{stem}.manifest.json"
    try:
        manifest = read_manifest(manifest_path)
    except ManifestError as error:
        raise StorageError(f"invalid manifest {manifest_path}: {error}") from error
    if manifest.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        raise StorageError(
            f"manifest uses unsupported schema version: {manifest.manifest_schema_version}"
        )
    if manifest.output != output_identity_for(parquet):
        raise StorageError(f"stale output identity for {parquet.name}")
    return manifest_path


def _validate_finalized_artifacts_strict(data_root):
    """Run :func:`validate_finalized_artifacts` followed by :func:`validate_geoparquet`.

    Use this when downstream code is about to load the validated
    Parquets and so the additional byte-level guarantees of
    :func:`validate_geoparquet` are required.
    """
    result = validate_finalized_artifacts(data_root)
    for parquet in result["parquets"]:
        validate_geoparquet(parquet)
    return result


def validate_finalized_artifacts_strict(data_root):
    """Validate finalized artifact pairs and every GeoParquet payload."""
    return _validate_finalized_artifacts_strict(data_root)


__all__ = [
    "StorageError",
    "validate_finalized_artifacts",
    "validate_finalized_artifacts_strict",
    "validate_geoparquet",
    "write_geoparquet",
]
