"""Atomic GeoParquet 1.1 writing.

A file is promoted only after streaming into an owned temporary, rewriting once
with final GeoParquet metadata (known only after the full pass), and passing
validation. Temporary files are confined to the output side and cleaned up
exactly. The rewrite pass streams batches through :meth:`ParquetFile.iter_batches`
so the per-PBF Parquet never loads entirely into memory.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.constants import DEFAULT_WRITE_BATCH_SIZE
from osm_polygon_description_tag.dataset.schema import (
    KEY_VALUE_COLUMNS,
    SCHEMA,
    geo_metadata,
    mapping_to_pairs,
)
from osm_polygon_description_tag.dataset.storage_validation import validate_geoparquet
from osm_polygon_description_tag.runtime.atomic import fsync_dir as _fsync_dir
from osm_polygon_description_tag.runtime.atomic import fsync_file

GEOPARQUET_COMPRESSION: Final = "zstd"

"""Codec every GeoParquet artifact is written with.

The dataset contract fixes the codec, so it is named once here rather
than spelled at each writer: four copies of a literal are four chances
for one of them to drift.
"""


DICTIONARY_COLUMNS = ["source_pbf", "osm_type", "geometry_type"]


@dataclass(frozen=True)
class _RecordStreamSummary:
    row_count: int
    geometry_types: frozenset[str]
    bbox: tuple[float, float, float, float] | None


def arrow_record(record: Mapping[str, object]) -> dict[str, object]:
    """Convert transform-layer mappings to the Hub-compatible Arrow shape."""
    normalized = dict(record)
    for column in KEY_VALUE_COLUMNS:
        normalized[column] = mapping_to_pairs(record.get(column))
    return normalized


def _owned_temp(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


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
        compression=GEOPARQUET_COMPRESSION,
        use_dictionary=DICTIONARY_COLUMNS,
    ) as writer:
        for batch in reader.iter_batches(batch_size=4096):
            writer.write_batch(batch)


def _record_bounds(record: Mapping[str, object]) -> tuple[float, float, float, float]:
    return (
        float(cast(float, record["bbox_min_x"])),
        float(cast(float, record["bbox_min_y"])),
        float(cast(float, record["bbox_max_x"])),
        float(cast(float, record["bbox_max_y"])),
    )


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
        batch.append(arrow_record(record))
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


_BBOX_COLUMNS = ("bbox_min_x", "bbox_min_y", "bbox_max_x", "bbox_max_y")


def _is_key_value_list(data_type: pa.DataType) -> bool:
    if not pa.types.is_list(data_type):
        return False
    entry = data_type.value_type
    return pa.types.is_struct(entry) and _is_string_pair_struct(entry)


def _is_string_pair_struct(entry: pa.StructType) -> bool:
    fields = [entry.field(index) for index in range(entry.num_fields)]
    return [(item.name, item.type) for item in fields] == [
        ("key", pa.string()),
        ("value", pa.string()),
    ]


def _pairs_are_canonical(array: pa.Array) -> bool:
    """Return whether ``mapping_to_pairs`` would leave every list unchanged.

    That holds when no list, key or value is null and keys strictly increase
    within each list (UTF-8 byte order equals Python code-point order).
    """
    if not _is_key_value_list(array.type) or array.null_count:
        return False
    entries = array.flatten()
    if entries.field("key").null_count or entries.field("value").null_count:
        return False
    return _keys_strictly_increase(array, entries.field("key"))


def _keys_strictly_increase(array: pa.ListArray, keys: pa.Array) -> bool:
    size = len(keys) - 1
    if size < 1:
        return True
    # Object arrays compare as Python strings, exactly as ``sorted`` does.
    key_values = keys.to_numpy(zero_copy_only=False)  # pragma: no mutate - None is non-zero-copy
    increasing = key_values[:-1] < key_values[1:]
    offsets = array.offsets.to_numpy()
    ends = offsets[1:] - offsets[0]
    ends = ends[(ends > 0) & (ends <= size)]
    increasing[ends - 1] = True
    return bool(increasing.all())


def _bounds_are_plain(array: pa.Array) -> bool:
    if array.type != pa.float64() or array.null_count:
        return False
    return not bool(np.isnan(array.to_numpy()).any())


def _fast_batch_is_exact(batch: pa.RecordBatch) -> bool:
    """Return whether casting ``batch`` equals the ``arrow_record`` round trip."""
    if batch.schema.names != SCHEMA.names or batch.column("geometry_type").null_count:
        return False
    return _columns_satisfy(batch, KEY_VALUE_COLUMNS, _pairs_are_canonical) and _columns_satisfy(
        batch, _BBOX_COLUMNS, _bounds_are_plain
    )


def _columns_satisfy(
    batch: pa.RecordBatch, names: Sequence[str], predicate: Callable[[pa.Array], bool]
) -> bool:
    return all(predicate(batch.column(name)) for name in names)


def _fast_batch(batch: pa.RecordBatch) -> pa.RecordBatch | None:
    """Cast ``batch`` to SCHEMA when that equals the ``arrow_record`` round trip."""
    if not _fast_batch_is_exact(batch):
        return None
    try:
        return pa.RecordBatch.from_arrays(
            [batch.column(name).cast(SCHEMA.field(name).type) for name in SCHEMA.names],
            schema=SCHEMA,
        )
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        return None


def _column_extreme(column: pa.Array, *, pick_min: bool) -> float:
    values = column.to_numpy()
    value = float(values.min() if pick_min else values.max())
    if value == 0.0:
        # Signed zeros compare equal; Python's min/max keep the first one seen.
        listed = column.to_pylist()
        return float(min(listed) if pick_min else max(listed))
    return value


def _batch_bounds(batch: pa.RecordBatch) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = (
        _column_extreme(batch.column(name), pick_min=name.startswith("bbox_min"))
        for name in _BBOX_COLUMNS
    )
    return min_x, min_y, max_x, max_y


@dataclass
class _BatchAccumulator:
    batch_size: int
    pending: list[pa.RecordBatch] = field(default_factory=list)
    pending_rows: int = 0
    geometry_types: set[str] = field(default_factory=set)
    bounds: tuple[float, float, float, float] | None = None
    row_count: int = 0

    def add(self, raw: pa.RecordBatch) -> None:
        if raw.num_rows == 0:
            return
        batch = _fast_batch(raw)
        if batch is None:
            batch = self._add_rows(raw.to_pylist())
        else:
            self.geometry_types.update(batch.column("geometry_type").unique().to_pylist())
            self.bounds = _merge_bounds(self.bounds, _batch_bounds(batch))
        self._pend(batch)

    def _add_rows(self, records: list[dict[str, Any]]) -> pa.RecordBatch:
        for record in records:
            self.geometry_types.add(str(record["geometry_type"]))
            self.bounds = _merge_bounds(self.bounds, _record_bounds(record))
        return pa.RecordBatch.from_pylist(
            [arrow_record(record) for record in records], schema=SCHEMA
        )

    def _pend(self, batch: pa.RecordBatch) -> None:
        self.row_count += batch.num_rows
        self.pending.append(batch)
        self.pending_rows += batch.num_rows

    def full_chunks(self) -> Iterable[pa.RecordBatch]:
        while self.pending_rows >= self.batch_size:
            combined = pa.Table.from_batches(self.pending, schema=SCHEMA).combine_chunks()
            yield from combined.slice(0, self.batch_size).to_batches()
            rest = combined.slice(self.batch_size)
            self.pending = rest.to_batches()
            self.pending_rows = rest.num_rows

    def remainder(self) -> pa.Table:
        return pa.Table.from_batches(self.pending, schema=SCHEMA).combine_chunks()


def _stream_batches(
    batches: Iterable[pa.RecordBatch],
    writer: pq.ParquetWriter,
    batch_size: int,
) -> _RecordStreamSummary:
    """Write ``batches`` re-chunked exactly as :func:`_stream_records` chunks rows."""
    accumulator = _BatchAccumulator(batch_size)
    for raw in batches:
        accumulator.add(raw)
        for chunk in accumulator.full_chunks():
            writer.write_batch(chunk)
    if accumulator.pending_rows:
        writer.write_table(accumulator.remainder())
    if accumulator.row_count == 0:
        writer.write_batch(pa.RecordBatch.from_pylist([], schema=SCHEMA))
    return _RecordStreamSummary(
        accumulator.row_count, frozenset(accumulator.geometry_types), accumulator.bounds
    )


def _require_batch_size(batch_size: int) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")


def write_geoparquet(
    records: Iterable[dict[str, object]],
    target: Path,
    *,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    validator: Callable[[Path], int] | None = None,
) -> int:
    """Stream ``records`` into a validated GeoParquet file atomically promoted to ``target``."""
    return _write_geoparquet_with(
        lambda writer: _stream_records(records, writer, batch_size),
        target,
        batch_size=batch_size,
        validator=validator,
    )


def write_geoparquet_batches(
    batches: Iterable[pa.RecordBatch],
    target: Path,
    *,
    batch_size: int = 1024,
    validator: Callable[[Path], int] | None = None,
) -> int:
    """Like :func:`write_geoparquet`, streaming Arrow batches instead of row mappings.

    The written file is byte-identical to writing ``batch.to_pylist()`` rows.
    """
    return _write_geoparquet_with(
        lambda writer: _stream_batches(batches, writer, batch_size),
        target,
        batch_size=batch_size,
        validator=validator,
    )


def _write_geoparquet_with(
    stream: Callable[[pq.ParquetWriter], _RecordStreamSummary],
    target: Path,
    *,
    batch_size: int,
    validator: Callable[[Path], int] | None,
) -> int:
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
            compression=GEOPARQUET_COMPRESSION,
            use_dictionary=DICTIONARY_COLUMNS,
        ) as writer:
            summary = stream(writer)

        bbox = list(summary.bbox) if summary.bbox is not None else []
        _stream_rewrite_with_metadata(
            temp_data,
            temp_final,
            geometry_types=sorted(summary.geometry_types),
            bbox=bbox,
        )

        validated_rows = validator(temp_final)
        fsync_file(temp_final)
        Path(temp_final).replace(target)
        _fsync_dir(target.parent)
        return validated_rows
    finally:
        for temp in (temp_data, temp_final):
            temp.unlink(missing_ok=True)


__all__ = ["write_geoparquet"]
