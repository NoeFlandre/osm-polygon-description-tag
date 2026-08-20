"""Atomic migration of legacy Arrow-map Parquets to Hub-compatible lists."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_description_tag.dataset.manifest import (
    TRANSFORM_ALGORITHM_VERSION,
    current_output_algorithm_revision,
    output_identity_for,
    read_manifest,
    write_manifest,
)
from osm_polygon_description_tag.dataset.schema import KEY_VALUE_COLUMNS, SCHEMA, SCHEMA_VERSION
from osm_polygon_description_tag.dataset.storage import (
    StorageError,
    _arrow_record,
    validate_geoparquet,
)


class MigrationError(RuntimeError):
    """Raised when a legacy artifact cannot be migrated safely."""


def _is_legacy_map_schema(schema: pa.Schema) -> bool:
    return all(
        schema.field(name).type == pa.map_(pa.string(), pa.string()) for name in KEY_VALUE_COLUMNS
    )


def _is_current_schema(schema: pa.Schema) -> bool:
    return schema.names == SCHEMA.names and all(
        schema.field(name).type == SCHEMA.field(name).type for name in SCHEMA.names
    )


def _migrate_parquet(path: Path) -> bool:
    reader = pq.ParquetFile(path)
    schema = reader.schema_arrow
    if not _requires_migration(schema, path):
        return False

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    metadata = SCHEMA.with_metadata(schema.metadata or {})
    try:
        _rewrite_legacy_parquet(reader, temporary, metadata)
        _promote_migrated_parquet(temporary, path)
        return True
    except (OSError, pa.ArrowException, StorageError) as error:
        raise MigrationError(f"cannot migrate {path}: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _requires_migration(schema: pa.Schema, path: Path) -> bool:
    if _is_current_schema(schema):
        return False
    if _is_legacy_map_schema(schema):
        return True
    raise MigrationError(f"unsupported schema for migration: {path}")


def _rewrite_legacy_parquet(
    reader: pq.ParquetFile,
    temporary: Path,
    metadata: pa.Schema,
) -> None:
    with pq.ParquetWriter(temporary, metadata, compression="zstd") as writer:
        for batch in reader.iter_batches(batch_size=4096):
            rows = [_arrow_record(row) for row in batch.to_pylist()]
            writer.write_table(pa.Table.from_pylist(rows, schema=metadata))
    validate_geoparquet(temporary)


def _promote_migrated_parquet(temporary: Path, target: Path) -> None:
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def migrate_dataset_schema(data_root: Path) -> int:
    """Migrate legacy map Parquets and matching manifests atomically.

    Each Parquet is promoted before its manifest is updated, so interruption
    leaves a safe per-file resume point. The raw PBF source root is never read
    or modified.
    """
    data_dir = data_root / "data"
    manifests_dir = data_root / "manifests"
    _require_migration_directories(data_dir, manifests_dir, data_root)
    migrated = 0
    for parquet in sorted(data_dir.glob("*.parquet"), key=lambda path: path.name):
        manifest_path = manifests_dir / f"{parquet.stem}.manifest.json"
        migrated += _migrate_one_artifact(parquet, manifest_path)
    return migrated


def _require_migration_directories(data_dir: Path, manifests_dir: Path, data_root: Path) -> None:
    if not data_dir.is_dir() or not manifests_dir.is_dir():
        raise MigrationError(f"missing data/ or manifests/ under {data_root}")


def _migrate_one_artifact(parquet: Path, manifest_path: Path) -> int:
    changed = _migrate_parquet(parquet)
    manifest = read_manifest(manifest_path)
    if changed or manifest.schema_version != SCHEMA_VERSION:
        write_manifest(
            replace(
                manifest,
                schema_version=SCHEMA_VERSION,
                transform_algorithm_version=TRANSFORM_ALGORITHM_VERSION,
                output_algorithm_revision=current_output_algorithm_revision(),
                output=output_identity_for(parquet),
            ),
            manifest_path,
        )
        return 1
    return 0


__all__ = ["MigrationError", "migrate_dataset_schema"]
